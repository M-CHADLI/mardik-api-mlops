"""Frontend client + pilotage, et les endpoints de la boucle d'enrichissement.

Deux pages servies par l'application elle-même, sans dépendance de build :

* ``/``         — le client juriste : coller un contrat, lire les clauses et
                  leur fiabilité. Il tape sur ``/analyse`` (la gateway) et non
                  sur ``/v2``, donc il voit exactement ce que voit un vrai
                  client, canary compris.
* ``/pilotage`` — le tableau de bord : trafic v1/v2, latence, erreurs, coût,
                  **distribution** du score, file de validation et journal.

Les pages sont du HTML statique lu sur disque plutôt que des gabarits : le
tableau de bord doit rester affichable quand le reste se dégrade, et une page
qui ne dépend que d'un ``fetch`` échoue de façon visible plutôt que par une
erreur 500 côté serveur.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.telemetry import MetricsStore
from ops.dashboard import resume
from ops.registry import Registry

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_WEB = RACINE / "web"
CONTRAT_EXEMPLE = RACINE / "eval" / "contrats" / "c12.txt"

router = APIRouter(tags=["web"])


def get_registry() -> Registry:
    return Registry()


def _page(nom: str) -> HTMLResponse:
    chemin = DOSSIER_WEB / nom
    if not chemin.exists():
        raise HTTPException(status_code=404, detail=f"page absente : {nom}")
    return HTMLResponse(chemin.read_text(encoding="utf-8"))


@router.get("/", response_class=HTMLResponse)
def accueil() -> HTMLResponse:
    return _page("index.html")


@router.get("/pilotage", response_class=HTMLResponse)
def pilotage() -> HTMLResponse:
    return _page("tableau-de-bord.html")


@router.get("/web/commun.css")
def feuille_de_style() -> Response:
    chemin = DOSSIER_WEB / "commun.css"
    if not chemin.exists():
        raise HTTPException(status_code=404, detail="feuille de style absente")
    return Response(chemin.read_text(encoding="utf-8"), media_type="text/css")


@router.get("/api/pilotage")
def api_pilotage(fenetre: float = 600, registry: Registry = Depends(get_registry)) -> dict[str, Any]:
    return resume(MetricsStore(), fenetre_s=fenetre, registry=registry)


@router.get("/api/exemple")
def api_exemple() -> dict[str, str]:
    """Un contrat long, pour que la démonstration montre la différence v1/v2."""
    if not CONTRAT_EXEMPLE.exists():
        raise HTTPException(status_code=404, detail="contrat d'exemple absent")
    return {"texte": CONTRAT_EXEMPLE.read_text(encoding="utf-8")}


@router.get("/api/captures")
def api_captures() -> dict[str, Any]:
    from eval.enrichissement import file_attente

    return {"attente": file_attente()}


class ValidationCapture(BaseModel):
    clauses_attendues: list[str] = Field(..., min_length=1)
    auteur: str = Field(..., min_length=2)


@router.post("/api/captures/{cas_id}/valider")
def api_valider(
    cas_id: str, corps: ValidationCapture, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    """Verse un cas relu dans le jeu d'évaluation — il sera rejoué au gate suivant."""
    from eval.enrichissement import valider

    try:
        return valider(cas_id, corps.clauses_attendues, corps.auteur, registry=registry)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class RejetCapture(BaseModel):
    auteur: str = Field(..., min_length=2)
    motif: str = Field(..., min_length=3)


@router.post("/api/captures/{cas_id}/rejeter")
def api_rejeter(
    cas_id: str, corps: RejetCapture, registry: Registry = Depends(get_registry)
) -> dict[str, str]:
    from eval.enrichissement import rejeter

    rejeter(cas_id, corps.auteur, corps.motif, registry=registry)
    return {"statut": "rejete", "cas_id": cas_id}
