"""Boucle 3 — enrichissement du jeu d'évaluation par les cas réels difficiles.

Une analyse dont la confiance passe sous le seuil est capturée, puis versée au
jeu d'évaluation où le gate la rejouera à la fusion suivante. C'est le réflexe
« un incident de production devient un test de non-régression », transposé au
modèle : le jeu d'éval cesse d'être un instantané figé pour devenir la mémoire
de ce que le produit a eu du mal à traiter.

Trois décisions à retenir.

**La capture est branchée sur la gateway, pas sur le moteur.** Si
``analyser_v2`` capturait lui-même, le gate d'évaluation — qui appelle le même
moteur — capturerait ses propres contrats à chaque exécution, et le jeu d'éval
grossirait en se recopiant. Seul le trafic réel alimente la boucle.

**Une validation humaine sépare la capture du versement.** Elle tient deux
rôles à la fois :

1. *Exactitude* — une confiance basse signifie que le modèle doute, pas qu'il
   s'est trompé. Verser sa sortie telle quelle comme vérité de référence
   graverait une erreur dans le jeu qui garde la chaîne.
2. *Confidentialité* — la pseudonymisation automatique traite ce qui a une
   forme reconnaissable mais laissera passer une raison sociale écrite au fil
   du texte. Le relecteur est le dernier contrôle avant que le cas ne parte
   dans Git et dans l'intégration continue.

**Le cas versé porte un ``seuil_note`` volontairement bas.** Un cas capturé
l'a été parce qu'il est difficile ; l'ajouter avec le seuil des contrats de
référence ferait échouer le gate dès la fusion suivante et pousserait l'équipe
à retirer le cas plutôt qu'à améliorer le modèle. Le jeu d'éval doit pouvoir
accueillir ce qui fait mal.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.llm_client import TYPES_CLAUSES
from eval.anonymisation import anonymiser
from ops.registry import Registry

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_CONTRATS = RACINE / "eval" / "contrats"
CHEMIN_ATTENDUS = RACINE / "eval" / "attendus.jsonl"
CHEMIN_CAPTURES = RACINE / "eval" / "captures.jsonl"

SEUIL_FAIBLE_CONFIANCE = 0.70
# Seuil appliqué aux cas issus de production : ils entrent pour être suivis,
# pas pour bloquer la livraison dès leur arrivée.
SEUIL_NOTE_CAPTURE = 0.5
PREFIXE_CAPTURE = "p"

_verrou = threading.Lock()


def chemin_captures(explicite: Path | None = None) -> Path:
    """File de validation. ``CAPTURES_PATH`` permet de l'isoler (tests, bac à sable)."""
    return Path(explicite or os.environ.get("CAPTURES_PATH") or CHEMIN_CAPTURES)


def chemin_contrats(explicite: Path | None = None) -> Path:
    return Path(explicite or os.environ.get("CONTRATS_PATH") or DOSSIER_CONTRATS)


def chemin_attendus(explicite: Path | None = None) -> Path:
    return Path(explicite or os.environ.get("ATTENDUS_PATH") or CHEMIN_ATTENDUS)


def _lire(chemin: Path) -> list[dict[str, Any]]:
    if not chemin.exists():
        return []
    return [
        json.loads(ligne)
        for ligne in chemin.read_text(encoding="utf-8").splitlines()
        if ligne.strip()
    ]


def _ajouter(chemin: Path, enregistrement: dict[str, Any]) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with _verrou, chemin.open("a", encoding="utf-8") as f:
        f.write(json.dumps(enregistrement, ensure_ascii=False) + "\n")


def capturer(
    texte: str,
    *,
    confiance: float,
    version: str,
    clauses_trouvees: list[str] | None = None,
    seuil: float | None = None,
    captures: Path | None = None,
    registry: Registry | None = None,
) -> dict[str, Any] | None:
    """Capture une analyse peu fiable. Sans effet si la confiance est au-dessus du seuil.

    Le contrat est pseudonymisé **avant** d'être écrit sur disque : le texte
    d'origine ne quitte jamais la mémoire du processus.
    """
    if seuil is None:
        # Seuil de pilotage : ajustable par `ops.deploy seuil faible_confiance <v>`
        # à partir des distributions observées, et chaque ajustement est tracé.
        from ops.deploy import charger_seuils

        seuil = float(charger_seuils()["faible_confiance"])
    if confiance >= seuil:
        return None

    texte_anonyme, substitutions = anonymiser(texte)
    cas = {
        "id": str(uuid.uuid4())[:8],
        "capture_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": version,
        "confiance_observee": round(confiance, 4),
        "clauses_trouvees": sorted(clauses_trouvees or []),
        "contrat": texte_anonyme,
        "anonymisation": substitutions,
        "statut": "a_valider",
    }
    _ajouter(chemin_captures(captures), cas)

    if registry is not None:
        registry.journaliser(
            "capture_faible_confiance",
            signal="score de confiance",
            valeur_observee=cas["confiance_observee"],
            seuil=seuil,
            cas_id=cas["id"],
            version=version,
            anonymisation=substitutions,
        )
    return cas


def _etat_cas(lignes: list[dict[str, Any]], cas_id: str) -> tuple[dict[str, Any] | None, str]:
    """La capture d'origine et le statut **effectif** d'un cas.

    Le fichier est un journal append-only : un même id porte sa capture, puis
    éventuellement un rejet, puis un versement. Ne lire que la première ligne
    laisserait verser dans le jeu d'évaluation — donc dans Git — un cas qu'un
    juriste a explicitement écarté. Le statut qui fait foi est le dernier écrit.
    """
    capture = next((c for c in lignes if c["id"] == cas_id and "contrat" in c), None)
    statuts = [c.get("statut") for c in lignes if c["id"] == cas_id and c.get("statut")]
    return capture, (statuts[-1] if statuts else "inconnu")


def file_attente(captures: Path | None = None) -> list[dict[str, Any]]:
    """Cas capturés en attente de relecture, sans le texte intégral du contrat."""
    lignes = _lire(chemin_captures(captures))
    traites = {c["id"] for c in lignes if c.get("statut") in {"rejete", "verse"}}
    vus: set[str] = set()
    attente: list[dict[str, Any]] = []
    for cas in reversed(lignes):
        if cas.get("statut") != "a_valider" or cas["id"] in vus or cas["id"] in traites:
            continue
        vus.add(cas["id"])
        attente.append(
            {**cas, "contrat": cas["contrat"][:400], "taille": len(cas["contrat"])}
        )
    return attente


def valider(
    cas_id: str,
    clauses_attendues: list[str],
    auteur: str,
    *,
    captures: Path | None = None,
    contrats: Path | None = None,
    attendus: Path | None = None,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """Verse un cas relu dans le jeu d'évaluation ; le gate le rejouera.

    Le texte versé est celui déjà pseudonymisé à la capture : la validation
    confirme, elle ne réintroduit jamais l'original.
    """
    captures, contrats, attendus = (
        chemin_captures(captures), chemin_contrats(contrats), chemin_attendus(attendus)
    )
    cas, statut = _etat_cas(_lire(captures), cas_id)
    if cas is None or statut != "a_valider":
        raise KeyError(f"cas inconnu ou déjà traité ({statut}) : {cas_id}")

    retenues = [c for c in clauses_attendues if c in TYPES_CLAUSES]
    if not retenues:
        raise ValueError("aucune clause attendue valide : le cas serait ininterprétable")

    contrat_id = f"{PREFIXE_CAPTURE}{cas_id}"
    contrats.mkdir(parents=True, exist_ok=True)
    (contrats / f"{contrat_id}.txt").write_text(cas["contrat"], encoding="utf-8")

    entree = {
        "contrat_id": contrat_id,
        "pages": max(1, round(len(cas["contrat"]) / 2000)),
        "clauses_attendues": retenues,
        "seuil_note": SEUIL_NOTE_CAPTURE,
        "origine": "production",
        "ajoute_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "valide_par": auteur,
        "confiance_observee": cas["confiance_observee"],
        "anonymisation": cas.get("anonymisation", {}),
    }
    _ajouter(attendus, entree)
    _ajouter(captures, {"id": cas_id, "statut": "verse", "contrat_id": contrat_id})

    if registry is not None:
        registry.journaliser(
            "enrichissement_jeu_evaluation",
            signal="validation humaine",
            auteur=auteur,
            cas_id=cas_id,
            contrat_id=contrat_id,
            clauses_attendues=retenues,
            valeur_observee=cas["confiance_observee"],
            seuil=SEUIL_FAIBLE_CONFIANCE,
        )
    return entree


def rejeter(
    cas_id: str,
    auteur: str,
    motif: str,
    *,
    captures: Path | None = None,
    registry: Registry | None = None,
) -> None:
    """Écarte un cas capturé : il n'entrera pas dans le jeu d'évaluation."""
    _ajouter(chemin_captures(captures), {"id": cas_id, "statut": "rejete", "motif": motif})
    if registry is not None:
        registry.journaliser(
            "capture_rejetee", signal="validation humaine", auteur=auteur,
            cas_id=cas_id, motif=motif,
        )
