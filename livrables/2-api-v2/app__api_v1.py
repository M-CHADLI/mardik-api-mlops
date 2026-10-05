"""Contrat historique ``/v1`` — [FOURNI], INTOUCHABLE.

C'est l'API que le client existant (``scripts/client_v1.py``) consomme
aujourd'hui. Elle ne doit **jamais** casser : ni le chemin, ni le corps de
requête, ni la forme de la réponse.

    POST /v1/analyse   {"texte": "<contrat>"}
    → 200 {"clauses": ["résiliation", ...], "modele": "...", "version": "v1.0.0",
           "tronque": true|false}
    → 422 corps invalide (détail explicite)
    → 503 fournisseur LLM indisponible (détail explicite)

Le contrat est envoyé en un seul appel (stratégie ``monolithique``). Si le
texte dépasse ``parametres.contexte_max_caracteres`` du bundle v1, il est
**coupé** avant l'appel : c'est la limite que la v2 doit lever.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.llm_client import TYPES_CLAUSES, Bundle, ErreurLLM, LLMClient
from app.telemetry import Mesure, Telemetry, build_default_telemetry

router = APIRouter(prefix="/v1", tags=["v1"])
VERSION_V1 = "v1"


class RequeteAnalyseV1(BaseModel):
    texte: str = Field(..., min_length=20, description="Texte intégral du contrat")


class ReponseAnalyseV1(BaseModel):
    clauses: list[str]
    modele: str
    version: str
    tronque: bool


def get_bundle_v1() -> Bundle:
    return Bundle.charger(VERSION_V1)


def get_client_v1(bundle: Bundle = Depends(get_bundle_v1)) -> LLMClient:
    return LLMClient(bundle)


def get_telemetry() -> Telemetry:
    return build_default_telemetry()


def analyser_v1(texte: str, client: LLMClient, telemetry: Telemetry) -> ReponseAnalyseV1:
    """Le cœur de la v1, réutilisable hors HTTP (tests, éval)."""
    bundle = client.bundle
    limite = int(bundle.parametres.get("contexte_max_caracteres", 16000))
    tronque = len(texte) > limite
    texte_envoye = texte[:limite]
    debut = time.perf_counter()
    with telemetry.tracer.start_as_current_span("analyse.requete") as span:
        span.set_attribute("mardik.version", bundle.version)
        span.set_attribute("mardik.tronque", tronque)
        try:
            with telemetry.tracer.start_as_current_span("llm.appel") as span_llm:
                reponse = client.completer(texte_envoye)
                span_llm.set_attribute("llm.latence_ms", reponse.latence_ms)
                span_llm.set_attribute("llm.tokens", reponse.tokens)
        except ErreurLLM as exc:
            telemetry.metriques.enregistrer(
                Mesure(
                    ts=time.time(),
                    version=bundle.version,
                    route="/v1/analyse",
                    latence_ms=(time.perf_counter() - debut) * 1000,
                    erreur=True,
                )
            )
            telemetry.logger.error("analyse.echec", version=bundle.version, cause=str(exc))
            raise
        clauses = _parser_clauses(reponse.texte)
        latence = (time.perf_counter() - debut) * 1000
        telemetry.metriques.enregistrer(
            Mesure(
                ts=time.time(),
                version=bundle.version,
                route="/v1/analyse",
                latence_ms=latence,
                score=None,
                cout_eur=client.cout_eur(reponse),
                appels_llm=1,
                tokens=reponse.tokens,
                tronque=tronque,
            )
        )
        telemetry.logger.info(
            "analyse.terminee",
            version=bundle.version,
            latence_ms=round(latence, 1),
            clauses=len(clauses),
            tronque=tronque,
        )
    return ReponseAnalyseV1(
        clauses=clauses, modele=bundle.modele, version=bundle.version, tronque=tronque
    )


def _parser_clauses(texte: str) -> list[str]:
    """Une clause par ligne ; on ne garde que les libellés connus, dédoublonnés."""
    connus = {t.lower(): t for t in TYPES_CLAUSES}
    resultat: list[str] = []
    for ligne in texte.splitlines():
        libelle = ligne.strip().lstrip("-*•0123456789.) ").strip().lower().rstrip(".")
        if libelle in connus and connus[libelle] not in resultat:
            resultat.append(connus[libelle])
    return resultat


@router.post("/analyse", response_model=ReponseAnalyseV1)
def analyse(
    requete: RequeteAnalyseV1,
    client: LLMClient = Depends(get_client_v1),
    telemetry: Telemetry = Depends(get_telemetry),
) -> ReponseAnalyseV1:
    try:
        return analyser_v1(requete.texte, client, telemetry)
    except ErreurLLM as exc:
        raise HTTPException(status_code=503, detail=f"fournisseur LLM indisponible : {exc}")
