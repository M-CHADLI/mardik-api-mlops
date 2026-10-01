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
import os
import subprocess
import sys
import time
from typing import Any

from app.llm_client import Bundle
from app.telemetry import MetricsStore
from ops.dashboard import agreger
from ops.registry import Registry

POURCENTAGE_CANARY_DEFAUT = 10


class ErreurDeploiement(RuntimeError):
    pass


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
    score_min: float = 0.7,
    taux_erreur_max: float = 0.10,
    latence_p95_max_ms: float = 8000,
    minimum: int = 10,
) -> dict[str, Any]:
    """Détecte la dérive de la version sous surveillance et retombe en arrière.

    La version observée est le canary s'il y en a un, sinon l'active : c'est la
    version « en cours de preuve ». ``minimum`` évite le réflexe le plus coûteux
    d'une boucle automatique — déclencher un rollback sur trois requêtes, au
    redémarrage ou dans un creux de trafic, alors qu'aucun signal n'est encore
    statistiquement lisible.
    """
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


def main(argv: list[str] | None = None) -> int:
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
