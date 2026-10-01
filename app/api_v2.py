"""Contrat ``/v2`` — la nouvelle version.

    POST /v2/analyse   {"texte": "<contrat>", "contrat_id": "c07" (optionnel)}
    → 200 {"clauses": [{"type", "extrait", "confiance", "sections"}, …],
           "confiance_globale", "modele", "version", "sections",
           "appels_llm", "latence_ms", "cout_eur"}
    → 422 corps invalide (détail explicite)
    → 503 fournisseur LLM indisponible (détail explicite)
    Jamais de 500 brut : toute erreur est explicite et journalisée.

La v1 fait un appel pour tout le contrat et coupe ce qui dépasse. La v2 fait
l'inverse : elle découpe le contrat (``pipeline.decouper``), analyse chaque
section (``pipeline.extraire``), fusionne (``pipeline.consolider``) puis score
(``pipeline.scorer``). Rien n'est tronqué — mais un contrat de 40 pages coûte
alors ~35 appels. **Ils partent de front** (``parametres.concurrence`` du
bundle) : en série, la contrainte client de 8 s serait hors d'atteinte, et
c'est le gate d'évaluation qui le dirait, pas la production.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, HTTPException
from opentelemetry import context as otel_context
from pydantic import BaseModel, Field

from app.llm_client import Bundle, ErreurLLM, LLMClient, ReponseLLM
from app.pipeline import Clause, Section, consolider, decouper, extraire, scorer
from app.telemetry import Mesure, Telemetry, build_default_telemetry

router = APIRouter(prefix="/v2", tags=["v2"])
VERSION_V2 = "v2"
ROUTE_V2 = "/v2/analyse"


class RequeteAnalyseV2(BaseModel):
    texte: str = Field(..., min_length=20, description="Texte intégral du contrat")
    contrat_id: str | None = None


class ClauseV2(BaseModel):
    type: str
    extrait: str
    confiance: float
    sections: list[int]


class ReponseAnalyseV2(BaseModel):
    clauses: list[ClauseV2]
    confiance_globale: float
    modele: str
    version: str
    sections: int
    appels_llm: int
    latence_ms: float
    cout_eur: float


def get_bundle_v2() -> Bundle:
    return Bundle.charger(VERSION_V2)


def get_client_v2(bundle: Bundle = Depends(get_bundle_v2)) -> LLMClient:
    return LLMClient(bundle)


def get_telemetry() -> Telemetry:
    return build_default_telemetry()


def analyser_v2(texte: str, client: LLMClient, telemetry: Telemetry) -> ReponseAnalyseV2:
    """Le cœur de la v2, réutilisable hors HTTP (le gate d'évaluation l'appelle)."""
    bundle = client.bundle
    debut = time.perf_counter()
    with telemetry.tracer.start_as_current_span("analyse.requete") as span:
        span.set_attribute("mardik.version", bundle.version)
        span.set_attribute("mardik.strategie", bundle.strategie)
        span.set_attribute("mardik.tronque", False)

        sections = decouper(texte, int(bundle.parametres.get("contexte_max_caracteres", 6000)))
        span.set_attribute("mardik.sections", len(sections))

        try:
            resultats = _analyser_sections(sections, client, telemetry)
        except ErreurLLM as exc:
            telemetry.metriques.enregistrer(
                Mesure(
                    ts=time.time(),
                    version=bundle.version,
                    route=ROUTE_V2,
                    latence_ms=(time.perf_counter() - debut) * 1000,
                    erreur=True,
                    appels_llm=len(sections),
                )
            )
            telemetry.logger.error("analyse.echec", version=bundle.version, cause=str(exc))
            raise

        clauses = consolider([clauses for clauses, _ in resultats])
        clauses, confiance_globale = scorer(clauses, texte)
        reponses = [reponse for _, reponse in resultats]
        latence = (time.perf_counter() - debut) * 1000
        cout = round(sum(client.cout_eur(r) for r in reponses), 6)
        tokens = sum(r.tokens for r in reponses)

        span.set_attribute("mardik.clauses", len(clauses))
        span.set_attribute("mardik.confiance_globale", confiance_globale)
        telemetry.metriques.enregistrer(
            Mesure(
                ts=time.time(),
                version=bundle.version,
                route=ROUTE_V2,
                latence_ms=latence,
                score=confiance_globale,
                cout_eur=cout,
                appels_llm=len(reponses),
                tokens=tokens,
                tronque=False,
            )
        )
        telemetry.logger.info(
            "analyse.terminee",
            version=bundle.version,
            latence_ms=round(latence, 1),
            sections=len(sections),
            clauses=len(clauses),
            confiance_globale=confiance_globale,
        )

    return ReponseAnalyseV2(
        clauses=[ClauseV2(**c.to_dict()) for c in clauses],
        confiance_globale=confiance_globale,
        modele=bundle.modele,
        version=bundle.version,
        sections=len(sections),
        appels_llm=len(reponses),
        latence_ms=round(latence, 1),
        cout_eur=cout,
    )


def _analyser_sections(
    sections: list[Section], client: LLMClient, telemetry: Telemetry
) -> list[tuple[list[Clause], ReponseLLM]]:
    """Un appel LLM par section, en parallèle, résultats remis dans l'ordre du contrat.

    Le contexte de trace est réattaché dans chaque thread : sans cela les spans
    ``llm.appel`` seraient orphelins et une analyse lente deviendrait illisible
    dans le tracing — exactement le défaut que la phase précédente a corrigé.
    """
    contexte = otel_context.get_current()

    def travailler(section: Section) -> tuple[list[Clause], ReponseLLM]:
        jeton = otel_context.attach(contexte)
        try:
            with telemetry.tracer.start_as_current_span("llm.appel") as span:
                span.set_attribute("mardik.section", section.indice)
                span.set_attribute("mardik.section_titre", section.titre)
                clauses, reponse = extraire(section, client)
                span.set_attribute("llm.latence_ms", reponse.latence_ms)
                span.set_attribute("llm.tokens", reponse.tokens)
                span.set_attribute("mardik.clauses", len(clauses))
                return clauses, reponse
        finally:
            otel_context.detach(jeton)

    concurrence = max(1, int(client.bundle.parametres.get("concurrence", 8)))
    if concurrence == 1 or len(sections) == 1:
        return [travailler(section) for section in sections]

    resultats: list[tuple[list[Clause], ReponseLLM] | None] = [None] * len(sections)
    pool = ThreadPoolExecutor(max_workers=min(concurrence, len(sections)))
    try:
        taches = {pool.submit(travailler, section): section.indice for section in sections}
        for tache in as_completed(taches):
            resultats[taches[tache]] = tache.result()
    except ErreurLLM:
        # Le fournisseur est tombé : inutile de laisser partir les sections restantes.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False)
    return [r for r in resultats if r is not None]


@router.post("/analyse", response_model=ReponseAnalyseV2)
def analyse(
    requete: RequeteAnalyseV2,
    client: LLMClient = Depends(get_client_v2),
    telemetry: Telemetry = Depends(get_telemetry),
) -> ReponseAnalyseV2:
    try:
        return analyser_v2(requete.texte, client, telemetry)
    except ErreurLLM as exc:
        raise HTTPException(status_code=503, detail=f"fournisseur LLM indisponible : {exc}")
    except Exception as exc:  # jamais de 500 muet : l'erreur est nommée et journalisée
        telemetry.logger.error("analyse.erreur_interne", cause=repr(exc))
        raise HTTPException(status_code=500, detail=f"erreur interne d'analyse : {exc}")
