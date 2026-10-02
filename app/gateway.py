"""Gateway : routeur canary entre les versions livrées.

    POST /analyse  {"texte": "..."}         → réponse de la version choisie,
                                              + en-tête ``X-Mardik-Version``
    GET  /gateway/etat                      → {"active", "canary", "canary_percent"}

    choisir_version(active, canary, canary_percent, tirage) -> str
        fonction pure : ``tirage`` ∈ [0, 100[ ; renvoie ``canary`` si un canary
        est déployé et ``tirage < canary_percent``, sinon ``active``.

Deux décisions à retenir ici :

* **l'état est lu à chaque requête**, jamais mis en cache. Une promotion ou un
  rollback écrit ``index.json`` ; si la gateway gardait l'état en mémoire, le
  « retour arrière immédiat » exigé par le client supposerait un redémarrage —
  c'est-à-dire une coupure, c'est-à-dire précisément ce qu'on veut éviter.
* **le tirage est un compteur, pas un hasard.** À 30 %, un tirage aléatoire
  donne « environ » 30 % — sur 40 requêtes de démonstration, il peut n'en
  router aucune et faire croire à une panne du canary. Un compteur cyclique
  sur 100 donne exactement 30 requêtes sur 100, immédiatement, ce qui rend la
  montée en charge observable et le comportement reproductible. Contrepartie
  assumée : un même client peut alterner v1/v2 d'une requête à l'autre (pas de
  session collante) — acceptable ici, les analyses sont indépendantes.
"""
from __future__ import annotations

import itertools
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.api_v1 import analyser_v1
from app.api_v2 import analyser_v2
from app.llm_client import ErreurLLM, LLMClient
from app.telemetry import Telemetry, build_default_telemetry
from ops.registry import Registry

router = APIRouter(tags=["gateway"])

# Moteurs disponibles, choisis par la *stratégie déclarée dans le bundle* et non
# par le numéro de version : une v3 « monolithique » retomberait d'elle-même sur
# le bon moteur, et une version livrée avec une stratégie inconnue est refusée
# plutôt que servie au hasard.
MOTEURS = {
    "monolithique": analyser_v1,
    "map_reduce_clauses": analyser_v2,
}

_compteur = itertools.count()


class RequeteAnalyse(BaseModel):
    texte: str = Field(..., min_length=20)
    contrat_id: str | None = None


def choisir_version(
    active: str, canary: str | None, canary_percent: int, tirage: float
) -> str:
    """Fonction pure : quelle version sert ce tirage ?"""
    if canary and canary_percent > 0 and tirage < canary_percent:
        return canary
    return active


def get_registry() -> Registry:
    return Registry()


def get_telemetry() -> Telemetry:
    return build_default_telemetry()


def pourcentage_canary(index: dict) -> int:
    """``CANARY_PERCENT`` force la valeur (démo) ; sinon celle de l'index."""
    forcee = os.environ.get("CANARY_PERCENT", "").strip()
    if forcee:
        try:
            return max(0, min(100, int(forcee)))
        except ValueError:
            pass
    return int(index.get("canary_percent") or 0)


@router.get("/gateway/etat")
def etat(registry: Registry = Depends(get_registry)) -> dict:
    index = registry.index()
    return {
        "active": index.get("active"),
        "canary": index.get("canary"),
        "canary_percent": pourcentage_canary(index),
        "precedente": index.get("precedente"),
        "versions": registry.versions(),
    }


@router.post("/analyse")
def analyse(
    requete: RequeteAnalyse,
    response: Response,
    registry: Registry = Depends(get_registry),
    telemetry: Telemetry = Depends(get_telemetry),
) -> dict:
    index = registry.index()
    active = index.get("active")
    if not active:
        raise HTTPException(status_code=503, detail="aucune version active dans le registre")

    version = choisir_version(
        active, index.get("canary"), pourcentage_canary(index), next(_compteur) % 100
    )
    response.headers["X-Mardik-Version"] = version

    # On sert le bundle *tel qu'il a été livré*, pas celui de models/ qui est en chantier.
    bundle = registry.bundle(version)
    moteur = MOTEURS.get(bundle.strategie)
    if moteur is None:
        raise HTTPException(
            status_code=503,
            detail=f"stratégie inconnue pour {version} : {bundle.strategie!r}",
        )

    debut = time.perf_counter()
    try:
        resultat = moteur(requete.texte, LLMClient(bundle), telemetry)
    except ErreurLLM as exc:
        raise HTTPException(status_code=503, detail=f"fournisseur LLM indisponible : {exc}")
    except HTTPException:
        raise
    except Exception as exc:  # jamais de 500 muet
        telemetry.logger.error("gateway.erreur_interne", version=version, cause=repr(exc))
        raise HTTPException(status_code=500, detail=f"erreur interne d'analyse : {exc}")

    # La gateway n'enregistre pas de mesure : le moteur en a déjà écrit une, avec
    # la version du bundle servi. En ajouter une seconde compterait chaque requête
    # deux fois au tableau de bord — et fausserait le taux d'erreur qui déclenche
    # le rollback automatique. La gateway route et trace ; elle ne mesure pas.
    telemetry.logger.info(
        "gateway.servi",
        version=version,
        canary=index.get("canary"),
        latence_ms=round((time.perf_counter() - debut) * 1000, 1),
    )

    corps = resultat.model_dump()
    _capturer_si_peu_fiable(corps, requete.texte, version, registry, telemetry)
    return corps


def _capturer_si_peu_fiable(
    corps: dict, texte: str, version: str, registry: Registry, telemetry: Telemetry
) -> None:
    """Boucle 3 : une analyse peu fiable servie en production nourrit le jeu d'éval.

    Branchée ici et pas dans ``analyser_v2`` : le gate d'évaluation appelle le
    même moteur, il capturerait donc ses propres contrats à chaque exécution et
    le jeu d'éval grossirait en se recopiant. Seul le trafic réel alimente la
    boucle.

    Une capture ne doit jamais faire échouer l'analyse qui l'a déclenchée : le
    client a sa réponse, l'enrichissement est un effet de bord best-effort.
    """
    confiance = corps.get("confiance_globale")
    if confiance is None:
        return  # la v1 ne produit pas de score : rien à capturer
    try:
        from eval.enrichissement import capturer

        capturer(
            texte,
            confiance=float(confiance),
            version=version,
            clauses_trouvees=[c["type"] for c in corps.get("clauses", [])],
            registry=registry,
        )
    except Exception as exc:
        telemetry.logger.warning("capture.echec", version=version, cause=repr(exc))
