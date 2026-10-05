"""Le client historique — [FOURNI]. Il ne doit JAMAIS casser.

C'est l'outil interne que l'équipe juridique de Mardik utilise aujourd'hui :
il envoie un contrat sur ``POST /v1/analyse`` et attend exactement le
contrat historique (``clauses`` = liste de libellés, ``modele``, ``version``).

    python scripts/client_v1.py [--url http://localhost:8000] [--contrat eval/contrats/c01.txt]

Code de sortie 0 si le contrat v1 est respecté, 1 sinon. Le test d'acceptance
« le client v1 fonctionne » appelle ``verifier()`` directement contre l'app.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

RACINE = Path(__file__).resolve().parent.parent
CONTRAT_DEFAUT = RACINE / "eval" / "contrats" / "c01.txt"


def verifier(poster: Callable[[str, dict[str, Any]], tuple[int, Any]], texte: str) -> list[str]:
    """Vérifie le contrat v1 ; renvoie la liste des manquements (vide = OK).

    ``poster(chemin, corps)`` → ``(status_code, json)`` — injecté pour pouvoir
    tester contre un ``TestClient`` sans réseau.
    """
    manquements: list[str] = []
    statut, corps = poster("/v1/analyse", {"texte": texte})
    if statut != 200:
        return [f"attendu 200, reçu {statut} : {corps}"]
    if not isinstance(corps, dict):
        return ["la réponse n'est pas un objet JSON"]
    for champ in ("clauses", "modele", "version"):
        if champ not in corps:
            manquements.append(f"champ manquant : {champ}")
    if not isinstance(corps.get("clauses"), list) or not all(
        isinstance(c, str) for c in corps.get("clauses", [])
    ):
        manquements.append("clauses doit être une liste de chaînes")
    if not str(corps.get("version", "")).startswith("v1"):
        manquements.append(f"version attendue v1.x, reçue {corps.get('version')!r}")

    statut, corps = poster("/v1/analyse", {"pas_le_bon_champ": 1})
    if statut != 422 or not (isinstance(corps, dict) and "detail" in corps):
        manquements.append(f"corps invalide : attendu 422 + detail, reçu {statut}")
    return manquements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Client historique Mardik (v1)")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--contrat", default=str(CONTRAT_DEFAUT))
    args = parser.parse_args(argv)

    import httpx

    texte = Path(args.contrat).read_text(encoding="utf-8")

    def poster(chemin: str, corps: dict[str, Any]) -> tuple[int, Any]:
        r = httpx.post(f"{args.url.rstrip('/')}{chemin}", json=corps, timeout=120)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    manquements = verifier(poster, texte)
    if manquements:
        print("CLIENT V1 CASSÉ :\n  - " + "\n  - ".join(manquements))
        return 1
    _, corps = poster("/v1/analyse", {"texte": texte})
    print(f"client v1 OK — {corps['version']} / {corps['modele']} — clauses : {corps['clauses']}")
    if corps.get("tronque"):
        print("  (contrat tronqué par la v1 : c'est la limite connue)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
