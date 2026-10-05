"""Extraction des clauses d'une section (un appel LLM, sortie JSON contrainte).

Contrat attendu :

    extraire(section: Section, client: LLMClient) -> tuple[list[Clause], ReponseLLM]

Deux familles d'échec, traitées différemment — c'est la décision structurante
de ce module :

* **le fournisseur est en panne** (HTTP, timeout, réponse vide) : ``ErreurLLM``
  remonte telle quelle. Insister section par section sur un service mort ne
  produit qu'une analyse amputée au prix de N timeouts ; l'appelant la traduit
  en 503 explicite.
* **le modèle répond mal** (JSON invalide, type de clause inconnu, confiance
  hors bornes) : la clause fautive est écartée, l'incident est signalé au log
  et à la trace, et l'analyse continue. Un modèle non déterministe produira
  toujours quelques sorties hors schéma ; les laisser casser l'analyse
  rendrait la v2 moins fiable que la v1.
"""
from __future__ import annotations

import structlog
from opentelemetry import trace

from app.llm_client import TYPES_CLAUSES, ErreurLLM, LLMClient, ReponseLLM
from app.pipeline.confiance import Clause
from app.pipeline.decoupage import Section

logger = structlog.get_logger("mardik.extraction")


def extraire(section: Section, client: LLMClient) -> tuple[list[Clause], ReponseLLM]:
    """Un appel LLM pour une section ; renvoie les clauses valides et la réponse brute."""
    reponse = client.completer(rediger_prompt(section), json_mode=True)
    return _lire_clauses(reponse, section), reponse


def rediger_prompt(section: Section) -> str:
    """Le prompt utilisateur : l'intitulé situe la section, le texte porte les clauses."""
    return f"Section : {section.titre}\n\n{section.texte.strip()}"


def _lire_clauses(reponse: ReponseLLM, section: Section) -> list[Clause]:
    try:
        charge = reponse.json()
    except ErreurLLM as exc:
        _signaler("reponse_non_json", section, cause=str(exc))
        return []

    brutes = charge.get("clauses") if isinstance(charge, dict) else charge
    if not isinstance(brutes, list):
        _signaler("schema_inattendu", section, cause=f"attendu une liste, reçu {type(brutes).__name__}")
        return []

    clauses: list[Clause] = []
    for brute in brutes:
        clause = _valider(brute, section)
        if clause is not None:
            clauses.append(clause)
    return clauses


def _valider(brute: object, section: Section) -> Clause | None:
    """Une clause ne survit que si elle respecte le schéma du bundle v2."""
    if not isinstance(brute, dict):
        _signaler("clause_non_objet", section, cause=repr(brute)[:80])
        return None
    type_clause = str(brute.get("type", "")).strip().lower()
    if type_clause not in TYPES_CLAUSES:
        _signaler("type_inconnu", section, cause=type_clause or "(vide)")
        return None
    extrait = str(brute.get("extrait", "")).strip()
    if not extrait:
        _signaler("extrait_vide", section, cause=type_clause)
        return None
    try:
        confiance = float(brute.get("confiance"))
    except (TypeError, ValueError):
        _signaler("confiance_non_numerique", section, cause=repr(brute.get("confiance"))[:40])
        return None
    if not 0.0 <= confiance <= 1.0:
        _signaler("confiance_hors_bornes", section, cause=str(confiance))
        return None
    return Clause(
        type=type_clause,
        extrait=extrait,
        confiance_llm=confiance,
        sections=[section.indice],
    )


def _signaler(incident: str, section: Section, *, cause: str) -> None:
    """Un écart au schéma n'est pas une panne : on le trace, on continue."""
    span = trace.get_current_span()
    span.add_event(
        "extraction.hors_schema",
        {"mardik.incident": incident, "mardik.section": section.indice, "mardik.cause": cause},
    )
    logger.warning(
        "extraction.hors_schema", incident=incident, section=section.indice, cause=cause
    )
