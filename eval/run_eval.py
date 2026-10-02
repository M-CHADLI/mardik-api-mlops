"""Le gate d'évaluation.

    evaluer(version, *, n_essais=None, seuil=0.75, latence_max_ms=8000,
            cout_max_eur=0.15, contrats=..., attendus=..., registry=None) -> Rapport

Le gate note une **version** (un bundle), pas du code : ``v1``/``v2`` désignent
un bundle en chantier dans ``models/``, ``v2.0.3`` une version déjà livrée,
relue depuis le registre. C'est ce qui permet de rejouer l'évaluation d'une
version livrée des mois plus tard, à l'identique.

La note est le **rappel** des clauses attendues : sur un contrat, ce qui coûte
cher au juriste n'est pas une clause en trop (il la voit et l'écarte) mais une
clause manquée — celle qu'il ne saura jamais qu'il a ratée. Une note qui
pénaliserait symétriquement les faux positifs récompenserait un modèle muet.

« Deux exécutions ne donnent pas la même note : que faites-vous ? »
Trois réponses cumulées, et aucune ne consiste à supprimer le non-déterminisme :

1. on le **réduit à la source** — ``temperature: 0`` et ``seed`` figés dans le
   bundle, donc versionnés comme le reste ;
2. on le **moyenne** — ``n_essais`` passes, la note retenue est leur moyenne ;
3. on le **mesure et on le montre** — ``dispersion_note`` est l'écart-type des
   notes entre passes. Un gate qui passe à 0,76 avec une dispersion de 0,05
   n'est pas un gate qui passe : il faut remonter le nombre de passes ou
   prendre une marge sur le seuil. La dispersion est dans le rapport et dans
   l'historique, donc comparable d'une version à l'autre.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.llm_client import Bundle, ErreurLLM, LLMClient
from app.telemetry import MetricsStore, NoopSpanExporter, Telemetry, build_telemetry
from ops.registry import MOTIF_VERSION, Registry

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_CONTRATS = RACINE / "eval" / "contrats"
CHEMIN_ATTENDUS = RACINE / "eval" / "attendus.jsonl"
CHEMIN_HISTORIQUE = RACINE / "eval" / "history.jsonl"
# Les mesures du gate ne vont PAS dans ops/metrics.jsonl : le tableau de bord
# et la surveillance de dérive lisent le trafic réel, pas les 12 analyses que
# la CI rejoue à chaque fusion.
CHEMIN_METRIQUES_EVAL = RACINE / "eval" / ".metrics_eval.jsonl"


@dataclass
class Rapport:
    version: str
    date: str
    essais: int
    note: float
    par_contrat: dict[str, dict[str, Any]]
    latence_p95_ms: float
    cout_moyen_eur: float
    passe: bool
    motifs: list[str] = field(default_factory=list)
    seuil: float = 0.75
    dispersion_note: float = 0.0
    # Confiance mesurée par le gate, conservée dans le manifeste à la publication.
    # C'est la **référence de dérive** : la promotion compare la confiance du
    # trafic réel à celle constatée quand la version a passé le gate, pas à une
    # moyenne glissante qui bougerait avec la dérive qu'elle est censée détecter.
    confiance_moyenne: float | None = None
    distribution_confiance: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def charger_attendus(chemin: Path = CHEMIN_ATTENDUS) -> dict[str, dict[str, Any]]:
    attendus: dict[str, dict[str, Any]] = {}
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        if ligne.strip():
            item = json.loads(ligne)
            attendus[item["contrat_id"]] = item
    return attendus


def charger_bundle(version: str, registry: Registry | None = None) -> Bundle:
    if MOTIF_VERSION.match(version):
        return (registry or Registry()).bundle(version)
    return Bundle.charger(version)


def _p95(valeurs: list[float]) -> float:
    if not valeurs:
        return 0.0
    tri = sorted(valeurs)
    return tri[min(len(tri) - 1, int(round(0.95 * len(tri) + 0.5)) - 1)]


def moteur_de(bundle: Bundle) -> Callable[..., Any]:
    """Le moteur découle de la stratégie déclarée par le bundle, jamais du numéro."""
    from app.api_v1 import analyser_v1
    from app.api_v2 import analyser_v2

    moteurs = {"monolithique": analyser_v1, "map_reduce_clauses": analyser_v2}
    moteur = moteurs.get(bundle.strategie)
    if moteur is None:
        raise ValueError(f"stratégie inconnue dans le bundle {bundle.version} : {bundle.strategie!r}")
    return moteur


def types_trouves(reponse: Any) -> set[str]:
    """Les clauses d'une réponse, que le moteur rende des libellés (v1) ou des objets (v2)."""
    clauses = getattr(reponse, "clauses", []) or []
    return {c if isinstance(c, str) else c.type for c in clauses}


def evaluer(
    version: str,
    *,
    n_essais: int | None = None,
    seuil: float = 0.75,
    latence_max_ms: float = 8000.0,
    cout_max_eur: float = 0.15,
    contrats: Path = DOSSIER_CONTRATS,
    attendus: Path = CHEMIN_ATTENDUS,
    registry: Registry | None = None,
    telemetry: Telemetry | None = None,
    sous_ensemble: list[str] | None = None,
    historique: Path | None = CHEMIN_HISTORIQUE,
) -> Rapport:
    """Note une version sur le jeu annoté et décide si elle peut être livrée."""
    bundle = charger_bundle(version, registry)
    moteur = moteur_de(bundle)
    essais = int(n_essais or bundle.parametres.get("essais_eval") or 1)
    references = charger_attendus(attendus)

    identifiants = sous_ensemble or sorted(
        chemin.stem for chemin in Path(contrats).glob("*.txt") if chemin.stem in references
    )

    telemetry, store = _preparer_telemetrie(telemetry)
    client = LLMClient(bundle)

    par_contrat: dict[str, dict[str, Any]] = {}
    latences: list[float] = []
    couts: list[float] = []
    notes_par_essai: list[list[float]] = [[] for _ in range(essais)]
    confiances: list[float] = []
    incidents: list[str] = []

    for cid in identifiants:
        reference = references[cid]
        texte = (Path(contrats) / f"{cid}.txt").read_text(encoding="utf-8")
        attendues = set(reference["clauses_attendues"])
        notes: list[float] = []
        trouvees: set[str] = set()

        for essai in range(essais):
            deja = len(store.lire())
            depart = time.perf_counter()
            try:
                reponse = moteur(texte, client, telemetry)
                trouvees = types_trouves(reponse)
            except ErreurLLM as exc:
                trouvees = set()
                incidents.append(f"{cid} : analyse en échec ({exc})")
            note = len(attendues & trouvees) / len(attendues) if attendues else 0.0
            notes.append(note)
            notes_par_essai[essai].append(note)

            nouvelles = store.lire()[deja:]
            latences.append(
                nouvelles[-1].latence_ms if nouvelles else (time.perf_counter() - depart) * 1000
            )
            couts.append(sum(m.cout_eur for m in nouvelles))
            confiances.extend(m.score for m in nouvelles if m.score is not None)

        seuil_note = float(reference.get("seuil_note", seuil))
        note_contrat = round(statistics.fmean(notes), 4)
        par_contrat[cid] = {
            "note": note_contrat,
            "seuil_note": seuil_note,
            "passe": note_contrat >= seuil_note,
            "trouvees": sorted(trouvees),
            "manquantes": sorted(attendues - trouvees),
            "latence_ms": round(statistics.fmean(latences[-essais:]), 1),
            "cout_eur": round(statistics.fmean(couts[-essais:]), 6),
        }

    note_globale = round(statistics.fmean(c["note"] for c in par_contrat.values()), 4) if par_contrat else 0.0
    notes_globales = [statistics.fmean(n) for n in notes_par_essai if n]
    dispersion = round(statistics.pstdev(notes_globales), 4) if len(notes_globales) > 1 else 0.0
    latence_p95 = round(_p95(latences), 1)
    cout_moyen = round(statistics.fmean(couts), 6) if couts else 0.0

    motifs: list[str] = []
    if note_globale < seuil:
        motifs.append(f"note {note_globale:.3f} < seuil {seuil}")
    recales = [cid for cid, c in par_contrat.items() if not c["passe"]]
    if recales:
        motifs.append("contrats sous leur seuil : " + ", ".join(recales))
    if latence_p95 >= latence_max_ms:
        motifs.append(f"latence P95 {latence_p95:.0f} ms >= plafond {latence_max_ms:.0f} ms")
    if cout_moyen >= cout_max_eur:
        motifs.append(f"coût moyen {cout_moyen:.4f} € >= plafond {cout_max_eur:.4f} €")
    motifs.extend(incidents)

    from ops.dashboard import distribuer

    rapport = Rapport(
        version=bundle.version,
        date=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        essais=essais,
        note=note_globale,
        par_contrat=par_contrat,
        latence_p95_ms=latence_p95,
        cout_moyen_eur=cout_moyen,
        passe=not motifs,
        motifs=motifs,
        seuil=seuil,
        dispersion_note=dispersion,
        confiance_moyenne=round(statistics.fmean(confiances), 4) if confiances else None,
        distribution_confiance=distribuer(confiances),
    )
    if historique is not None:
        _archiver(rapport, Path(historique))
    return rapport


def _preparer_telemetrie(telemetry: Telemetry | None) -> tuple[Telemetry, MetricsStore]:
    """Le gate réutilise la télémétrie de production : il mesure ce que la prod mesure.

    Latence et coût ne sont pas recalculés ici à partir d'un chronomètre maison ;
    ils sont relus dans le ``MetricsStore`` alimenté par les moteurs eux-mêmes.
    Si le gate et la production ne mesuraient pas la même chose, un gate vert ne
    dirait rien du comportement réel.
    """
    if telemetry is not None:
        return telemetry, telemetry.metriques
    CHEMIN_METRIQUES_EVAL.unlink(missing_ok=True)
    telemetry = build_telemetry(
        span_exporter=NoopSpanExporter(),
        metrics_path=CHEMIN_METRIQUES_EVAL,
        level="WARNING",
        service_name="mardik-eval",
    )
    return telemetry, telemetry.metriques


def _archiver(rapport: Rapport, chemin: Path) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rapport.to_dict(), ensure_ascii=False) + "\n")


def afficher(rapport: Rapport) -> None:
    print(f"== Gate d'évaluation — {rapport.version} ({rapport.essais} essai(s)) ==")
    for cid, c in rapport.par_contrat.items():
        etat = "OK " if c["passe"] else "KO "
        manque = f"  manquantes: {', '.join(c['manquantes'])}" if c["manquantes"] else ""
        print(f"  {etat} {cid}  note={c['note']:.2f}  (seuil {c['seuil_note']}){manque}")
    print(
        f"note globale = {rapport.note:.3f} | P95 = {rapport.latence_p95_ms:.0f} ms"
        f" | coût moyen = {rapport.cout_moyen_eur:.4f} €"
        f" | dispersion = {rapport.dispersion_note:.3f}"
    )
    if rapport.confiance_moyenne is not None:
        print(
            f"confiance de référence = {rapport.confiance_moyenne:.3f}  "
            + "  ".join(f"{k}: {v}" for k, v in rapport.distribution_confiance.items())
        )
    print("GATE : " + ("PASSE" if rapport.passe else "ÉCHEC — " + " ; ".join(rapport.motifs)))


def _console_tolerante() -> None:
    """Une console Windows en cp1252 ne doit pas faire planter un outil d'astreinte."""
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _console_tolerante()
    parser = argparse.ArgumentParser(description="Gate d'évaluation Mardik")
    parser.add_argument("--version", default="v2")
    parser.add_argument("--seuil", type=float, default=0.75)
    parser.add_argument("--essais", type=int, default=None)
    parser.add_argument("--latence-max-ms", type=float, default=8000)
    parser.add_argument("--cout-max-eur", type=float, default=0.15)
    parser.add_argument("--contrats", default=None, help="liste c01,c02,… (défaut : tous)")
    args = parser.parse_args(argv)
    sous_ensemble = re.split(r"[,\s]+", args.contrats.strip()) if args.contrats else None
    rapport = evaluer(
        args.version,
        n_essais=args.essais,
        seuil=args.seuil,
        latence_max_ms=args.latence_max_ms,
        cout_max_eur=args.cout_max_eur,
        sous_ensemble=sous_ensemble,
    )
    afficher(rapport)
    return 0 if rapport.passe else 1


if __name__ == "__main__":
    sys.exit(main())
