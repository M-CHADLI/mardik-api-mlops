"""Déploiement : publication, canary, promotion, rollback, surveillance.

Le registre (``ops/registry``) enregistre ; ce module décide. Toute décision
passe par ``index.json`` et laisse une entrée au journal : qui a changé quoi,
quand, et sur quel signal. C'est l'exigence « tout ajustement est tracé » — et
accessoirement la seule façon, après coup, de répondre à « pourquoi la prod
est-elle revenue en v1 cette nuit ? ».

    publier(version, *, bundle="v2", commit=None, registry=None, seuil=0.75,
            rapport=None) -> manifest
    deployer_canary(version, pourcentage=None, registry=None) -> index
    promouvoir(version, registry=None) -> index
    rollback(registry=None, motif="manuel") -> index
    surveiller(registry=None, metriques=None, *, fenetre_s=120, score_min=0.7,
               taux_erreur_max=0.10, latence_p95_max_ms=8000, minimum=10) -> dict

Ligne de commande : ``python -m ops.deploy publier v2.0.0 | canary v2.0.0 --pourcentage 10
| promouvoir v2.0.0 | rollback | surveiller [--boucle]``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.llm_client import Bundle
from app.telemetry import MetricsStore
from ops.dashboard import agreger
from ops.registry import Registry

POURCENTAGE_CANARY_DEFAUT = 10
# Paliers de montée en charge du canary. Trois paliers plutôt que deux : entre
# 10 % (assez peu pour qu'un incident reste contenu) et 100 %, 30 % donne un
# volume suffisant pour que la distribution du score devienne lisible sans
# exposer la majorité des juristes.
PALIERS_CANARY: tuple[int, ...] = (10, 30, 100)

CHEMIN_SEUILS = Path(__file__).resolve().parent / "seuils.json"
SEUILS_DEFAUT: dict[str, float] = {
    "score_min": 0.70,
    "taux_erreur_max": 0.10,
    "latence_p95_max_ms": 8000.0,
    "minimum_mesures": 10,
    "faible_confiance": 0.70,
    "minimum_promotion": 30,
    "marge_promotion": 0.05,
}


class ErreurDeploiement(RuntimeError):
    pass


def charger_seuils(chemin: Path | None = None) -> dict[str, float]:
    """Les seuils effectifs : les valeurs ajustées écrasent les valeurs par défaut.

    Les seuils vivent dans un fichier, pas dans le code : les ajuster à partir
    des distributions observées est une opération d'exploitation, pas une
    livraison. Chaque ajustement passe par ``ajuster_seuil`` et laisse une trace.
    """
    chemin = Path(chemin or os.environ.get("SEUILS_PATH") or CHEMIN_SEUILS)
    seuils = dict(SEUILS_DEFAUT)
    if chemin.exists():
        try:
            seuils.update(json.loads(chemin.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return seuils


def ajuster_seuil(
    nom: str,
    valeur: float,
    *,
    auteur: str,
    motif: str,
    registry: Registry | None = None,
    chemin: Path | None = None,
) -> dict[str, float]:
    """Change un seuil de pilotage et l'inscrit au journal.

    L'exigence du CTO — « tout ajustement de la chaîne est tracé : quel signal
    l'a déclenché, quand, par qui ou par quoi » — s'applique aussi aux seuils.
    Un seuil qu'on remonte discrètement parce que l'alerte sonne trop souvent
    est la façon la plus courante de désarmer une surveillance sans le dire.
    """
    if nom not in SEUILS_DEFAUT:
        raise ErreurDeploiement(f"seuil inconnu : {nom} (connus : {', '.join(SEUILS_DEFAUT)})")
    chemin = Path(chemin or os.environ.get("SEUILS_PATH") or CHEMIN_SEUILS)
    seuils = charger_seuils(chemin)
    ancienne = seuils[nom]
    seuils[nom] = valeur
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(seuils, ensure_ascii=False, indent=2), encoding="utf-8")

    (registry or Registry()).journaliser(
        "ajustement_seuil",
        signal=nom,
        auteur=auteur,
        motif=motif,
        avant={nom: ancienne},
        apres={nom: valeur},
    )
    return seuils


def _commit_courant() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "local"


def _etat(registry: Registry) -> dict[str, Any]:
    index = registry.index()
    return {
        "active": index.get("active"),
        "canary": index.get("canary"),
        "canary_percent": int(index.get("canary_percent") or 0),
    }


def publier(
    version: str,
    *,
    bundle: str = "v2",
    commit: str | None = None,
    registry: Registry | None = None,
    seuil: float = 0.75,
    rapport: Any | None = None,
) -> dict[str, Any]:
    """Étiquette une version — si et seulement si le gate d'évaluation passe.

    Le gate est joué ici plutôt qu'en amont pour qu'il soit impossible de
    déposer un artefact non évalué dans le registre, quel que soit le chemin
    emprunté (CI, console, script). Un ``rapport`` déjà calculé peut être
    fourni pour ne pas rejouer 12 analyses, mais il est vérifié de la même
    façon : c'est la décision du gate qui bloque, pas l'endroit où il a tourné.
    """
    registre = registry or Registry()
    if rapport is None:
        from eval.run_eval import evaluer

        rapport = evaluer(bundle, seuil=seuil, registry=registre)
    if not getattr(rapport, "passe", False):
        motifs = " ; ".join(getattr(rapport, "motifs", []) or ["gate en échec"])
        raise ErreurDeploiement(f"gate d'évaluation en échec pour {version} : {motifs}")

    artefact = Bundle.charger(bundle)
    manifest = registre.etiqueter(
        version,
        artefact,
        commit=commit or _commit_courant(),
        note_eval=rapport.note,
        details={
            "seuil": getattr(rapport, "seuil", seuil),
            "essais_eval": getattr(rapport, "essais", 1),
            "dispersion_note": getattr(rapport, "dispersion_note", 0.0),
            "latence_p95_ms": getattr(rapport, "latence_p95_ms", None),
            "cout_moyen_eur": getattr(rapport, "cout_moyen_eur", None),
            # Référence de dérive, figée au moment du gate (cf. promouvoir_si_conforme).
            "confiance_moyenne": getattr(rapport, "confiance_moyenne", None),
            "distribution_confiance": getattr(rapport, "distribution_confiance", {}),
        },
    )
    registre.journaliser(
        "publication",
        version=version,
        commit=manifest["commit"],
        empreinte=manifest["empreinte"],
        note_eval=rapport.note,
        avant=_etat(registre),
        apres=_etat(registre),
    )
    return manifest


def deployer_canary(
    version: str, pourcentage: int | None = None, registry: Registry | None = None
) -> dict[str, Any]:
    """Route ``pourcentage`` % du trafic vers ``version`` sans toucher à l'active."""
    registre = registry or Registry()
    avant = _etat(registre)
    if pourcentage is None:
        pourcentage = int(os.environ.get("CANARY_PERCENT") or POURCENTAGE_CANARY_DEFAUT)
    if version == avant["active"]:
        raise ErreurDeploiement(f"{version} est déjà la version active")
    registre.definir_canary(version, pourcentage)
    registre.journaliser(
        "canary", version=version, pourcentage=pourcentage, avant=avant, apres=_etat(registre)
    )
    return registre.index()


def promouvoir(version: str, registry: Registry | None = None) -> dict[str, Any]:
    """La version prend 100 % du trafic ; l'ancienne active devient le filet de secours."""
    registre = registry or Registry()
    registre.manifest(version)  # refuse une version inconnue du registre
    avant = _etat(registre)
    precedente = avant["active"]

    registre.definir_actif(version)
    registre.definir_canary(None, 0)
    index = registre.index()
    # Sans ``precedente``, un rollback après promotion n'aurait nulle part où revenir.
    if precedente and precedente != version:
        index["precedente"] = precedente
        registre.ecrire_index(index)
    registre.journaliser(
        "promotion", version=version, precedente=precedente, avant=avant, apres=_etat(registre)
    )
    return registre.index()


def rollback(registry: Registry | None = None, motif: str = "manuel") -> dict[str, Any]:
    """Retour arrière en **une** opération, sans redémarrage ni correctif.

    Deux situations, une seule commande pour l'exploitant : si un canary est en
    cours, le retirer suffit (le trafic retombe à 100 % sur l'active) ; sinon on
    rend la main à ``precedente``. Demander à l'astreinte de choisir entre deux
    commandes à 3 h du matin, c'est transformer un rollback en incident.
    """
    registre = registry or Registry()
    avant = _etat(registre)
    canary, _ = registre.canary()

    if canary:
        registre.definir_canary(None, 0)
    else:
        precedente = registre.index().get("precedente")
        if not precedente:
            raise ErreurDeploiement("aucune version précédente connue : rollback impossible")
        registre.definir_actif(precedente)
        index = registre.index()
        index["precedente"] = avant["active"]
        registre.ecrire_index(index)

    registre.journaliser("rollback", motif=motif, avant=avant, apres=_etat(registre))
    return registre.index()


def surveiller(
    registry: Registry | None = None,
    metriques: MetricsStore | None = None,
    *,
    fenetre_s: float = 120,
    score_min: float | None = None,
    taux_erreur_max: float | None = None,
    latence_p95_max_ms: float | None = None,
    minimum: int | None = None,
) -> dict[str, Any]:
    """Détecte la dérive de la version sous surveillance et retombe en arrière.

    La version observée est le canary s'il y en a un, sinon l'active : c'est la
    version « en cours de preuve ». ``minimum`` évite le réflexe le plus coûteux
    d'une boucle automatique — déclencher un rollback sur trois requêtes, au
    redémarrage ou dans un creux de trafic, alors qu'aucun signal n'est encore
    statistiquement lisible.

    Les seuils non précisés viennent de ``ops/seuils.json`` (cf. ``ajuster_seuil``) :
    l'astreinte peut les resserrer sans relivrer, et chaque ajustement est tracé.
    """
    seuils = charger_seuils()
    score_min = seuils["score_min"] if score_min is None else score_min
    taux_erreur_max = seuils["taux_erreur_max"] if taux_erreur_max is None else taux_erreur_max
    latence_p95_max_ms = (
        seuils["latence_p95_max_ms"] if latence_p95_max_ms is None else latence_p95_max_ms
    )
    minimum = int(seuils["minimum_mesures"] if minimum is None else minimum)
    registre = registry or Registry()
    store = metriques or MetricsStore()
    canary, _ = registre.canary()
    version = canary or registre.active()

    resultat: dict[str, Any] = {
        "version": version,
        "mesures": 0,
        "derive": False,
        "motif": "",
        "rollback": False,
    }
    if not version:
        return resultat

    mesures = store.lire(depuis_s=fenetre_s, version=version)
    resultat["mesures"] = len(mesures)
    if len(mesures) < minimum:
        return resultat

    indicateurs = agreger(mesures)
    resultat["indicateurs"] = indicateurs
    motifs: list[str] = []
    score_moyen = indicateurs["score_moyen"]
    if score_moyen is not None and score_moyen < score_min:
        motifs.append(f"score de confiance moyen {score_moyen:.3f} < {score_min}")
    if indicateurs["taux_erreur"] > taux_erreur_max:
        motifs.append(f"taux d'erreur {indicateurs['taux_erreur']:.1%} > {taux_erreur_max:.1%}")
    if indicateurs["latence_p95_ms"] > latence_p95_max_ms:
        motifs.append(
            f"latence P95 {indicateurs['latence_p95_ms']:.0f} ms > {latence_p95_max_ms:.0f} ms"
        )
    if not motifs:
        return resultat

    resultat["derive"] = True
    resultat["motif"] = " ; ".join(motifs)
    rollback(registre, motif=resultat["motif"])
    resultat["rollback"] = True
    return resultat


def promouvoir_si_conforme(
    registry: Registry | None = None,
    metriques: MetricsStore | None = None,
    *,
    fenetre_s: float = 600,
    minimum: int | None = None,
    marge: float | None = None,
    auteur: str = "automatique",
) -> dict[str, Any]:
    """Boucle 2 — monte le canary d'un palier si la fenêtre d'observation est concluante.

    Trois conditions cumulées, dans cet ordre :

    1. **assez de trafic** — en dessous de ``minimum_promotion`` mesures, aucune
       distribution n'est lisible et promouvoir reviendrait à tirer à pile ou face ;
    2. **aucune condition de rollback remplie** — on ne promeut pas une version
       qu'on serait en train de retirer ;
    3. **la confiance tient la référence du gate** — la confiance moyenne observée
       en production doit atteindre celle mesurée quand la version a passé le gate,
       à ``marge`` près.

    La condition 3 répond à « dérive par rapport à quoi ». La référence est la
    distribution constatée **à la sortie de la chaîne**, figée dans le manifeste,
    et non une moyenne glissante : une référence glissante absorberait lentement
    la dérive qu'elle est censée détecter, et finirait par la valider.

    Un refus est journalisé au même titre qu'une promotion. Savoir pourquoi la v2
    n'est **pas** montée est aussi utile que de savoir pourquoi elle est montée —
    et c'est ce qui manque le plus souvent quand une migration stagne sans que
    personne ne sache dire sur quel critère elle bloque.
    """
    registre = registry or Registry()
    store = metriques or MetricsStore()
    seuils = charger_seuils()
    minimum = int(seuils["minimum_promotion"] if minimum is None else minimum)
    marge = float(seuils["marge_promotion"] if marge is None else marge)

    canary, pourcentage = registre.canary()
    resultat: dict[str, Any] = {
        "version": canary,
        "pourcentage": pourcentage,
        "mesures": 0,
        "promue": False,
        "palier": None,
        "motif": "",
    }
    if not canary:
        resultat["motif"] = "aucun canary en cours"
        return resultat

    mesures = store.lire(depuis_s=fenetre_s, version=canary)
    resultat["mesures"] = len(mesures)
    if len(mesures) < minimum:
        resultat["motif"] = f"fenêtre incomplète : {len(mesures)}/{minimum} mesures"
        return resultat

    diagnostic = surveiller(
        registre, store, fenetre_s=fenetre_s, minimum=minimum, score_min=seuils["score_min"]
    )
    if diagnostic["derive"]:
        resultat["motif"] = f"dérive en cours : {diagnostic['motif']}"
        return resultat

    indicateurs = agreger(mesures)
    resultat["indicateurs"] = indicateurs
    reference = registre.manifest(canary).get("confiance_moyenne")
    observee = indicateurs["score_moyen"]
    if reference is not None and observee is not None and observee < reference - marge:
        resultat["motif"] = (
            f"confiance observée {observee:.3f} < référence du gate "
            f"{reference:.3f} (marge {marge})"
        )
        registre.journaliser(
            "promotion_refusee",
            version=canary,
            signal="score de confiance",
            auteur=auteur,
            valeur_observee=observee,
            seuil=round(reference - marge, 4),
            motif=resultat["motif"],
            fenetre=len(mesures),
        )
        return resultat

    suivant = palier_suivant(pourcentage)
    if suivant is None or suivant >= 100:
        index = promouvoir(canary, registry=registre)
        resultat.update(promue=True, palier=100, motif="promotion totale", index=index)
        return resultat

    registre.definir_canary(canary, suivant)
    registre.journaliser(
        "promotion_canary",
        version=canary,
        signal="score de confiance",
        auteur=auteur,
        valeur_observee=observee,
        seuil=reference,
        avant={"canary_percent": pourcentage},
        apres={"canary_percent": suivant},
        fenetre=len(mesures),
    )
    resultat.update(
        promue=True, palier=suivant, motif=f"palier {pourcentage} % -> {suivant} %"
    )
    return resultat


def palier_suivant(pourcentage: int) -> int | None:
    """Le palier au-dessus de ``pourcentage`` dans ``PALIERS_CANARY``."""
    for palier in PALIERS_CANARY:
        if palier > pourcentage:
            return palier
    return None


def _console_tolerante() -> None:
    """Une console Windows en cp1252 ne doit pas faire planter un outil d'astreinte."""
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _console_tolerante()
    parser = argparse.ArgumentParser(description="Déploiement Mardik")
    sub = parser.add_subparsers(dest="commande", required=True)
    p = sub.add_parser("publier")
    p.add_argument("version")
    p.add_argument("--bundle", default="v2")
    p.add_argument("--seuil", type=float, default=0.75)
    c = sub.add_parser("canary")
    c.add_argument("version")
    c.add_argument("--pourcentage", type=int, default=None)
    pr = sub.add_parser("promouvoir")
    pr.add_argument("version")
    r = sub.add_parser("rollback")
    r.add_argument("--motif", default="manuel")
    pa = sub.add_parser("promouvoir-auto")
    pa.add_argument("--fenetre", type=float, default=600)
    pa.add_argument("--minimum", type=int, default=None)
    pa.add_argument("--auteur", default="automatique")
    sl = sub.add_parser("seuil")
    sl.add_argument("nom")
    sl.add_argument("valeur", type=float)
    sl.add_argument("--auteur", required=True)
    sl.add_argument("--motif", required=True)
    s = sub.add_parser("surveiller")
    s.add_argument("--boucle", action="store_true")
    s.add_argument("--intervalle", type=float, default=5.0)
    s.add_argument("--fenetre", type=float, default=120)
    args = parser.parse_args(argv)

    try:
        if args.commande == "publier":
            print(publier(args.version, bundle=args.bundle, seuil=args.seuil))
        elif args.commande == "canary":
            print(deployer_canary(args.version, args.pourcentage))
        elif args.commande == "promouvoir":
            print(promouvoir(args.version))
        elif args.commande == "rollback":
            print(rollback(motif=args.motif))
        elif args.commande == "promouvoir-auto":
            print(
                promouvoir_si_conforme(
                    fenetre_s=args.fenetre, minimum=args.minimum, auteur=args.auteur
                )
            )
        elif args.commande == "seuil":
            print(
                ajuster_seuil(args.nom, args.valeur, auteur=args.auteur, motif=args.motif)
            )
        elif args.commande == "surveiller":
            while True:
                res = surveiller(fenetre_s=args.fenetre)
                print(res)
                if not args.boucle or res["rollback"]:
                    break
                time.sleep(args.intervalle)
    except ErreurDeploiement as exc:
        print(f"REFUSÉ : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
