"""Assemble les livrables du brief dans `livrables/`, à partir des sources.

Le dossier `livrables/` est un **instantané** destiné au dépôt : il regroupe,
sous les cinq intitulés du brief, des copies des fichiers qui vivent ailleurs
dans le dépôt. Une copie diverge de sa source dès la première correction — d'où
ce script, qui la régénère au lieu de la rafistoler :

    python -m livrables.assembler

La source fait foi. En cas de doute entre un fichier de `livrables/` et son
original, c'est l'original qui compte.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
CIBLE = RACINE / "livrables"

# Chaque livrable du brief → les fichiers qui le constituent.
LIVRABLES: dict[str, list[str]] = {
    "1-dossier-de-conception": [
        "docs/dossier-de-conception.md",
        "docs/besoin_client.md",
    ],
    "2-api-v2": [
        "app/api_v2.py",
        "app/pipeline/decoupage.py",
        "app/pipeline/extraction.py",
        "app/pipeline/consolidation.py",
        "app/pipeline/confiance.py",
        "app/gateway.py",
        "app/api_v1.py",
        "models/v2/config.yaml",
        "models/v1/config.yaml",
        "scripts/client_v1.py",
    ],
    "3-chaine-llmops": [
        ".github/workflows/llmops.yml",
        "ops/deploy.py",
        "eval/run_eval.py",
        "ops/registry/__init__.py",
        "ops/registry/index.json",
        "Dockerfile",
        "Makefile",
    ],
    "4-observabilite": [
        "ops/dashboard.py",
        "eval/enrichissement.py",
        "eval/anonymisation.py",
        "app/telemetry.py",
        "docs/exploitation.md",
    ],
    "5-frontend": [
        "web/index.html",
        "web/tableau-de-bord.html",
        "web/commun.css",
        "app/frontend.py",
        "render.yaml",
        "scripts/demarrer.sh",
    ],
    "6-tests": [
        "tests/acceptance/test_chaine.py",
        "tests/acceptance/test_observabilite.py",
        "tests/acceptance/test_boucles.py",
        "tests/conftest.py",
    ],
}


def commit_courant() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, cwd=RACINE,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "inconnu"


def assembler() -> dict[str, int]:
    comptes: dict[str, int] = {}
    for dossier, fichiers in LIVRABLES.items():
        destination = CIBLE / dossier
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for relatif in fichiers:
            source = RACINE / relatif
            if not source.exists():
                raise FileNotFoundError(f"source absente : {relatif}")
            # Le chemin d'origine est aplati mais conservé dans le nom, pour
            # qu'un fichier déposé reste rattachable à sa place dans le dépôt.
            shutil.copy(source, destination / relatif.replace("/", "__"))
        comptes[dossier] = len(fichiers)

    (CIBLE / "INSTANTANE.txt").write_text(
        f"Instantané des livrables\n"
        f"commit  : {commit_courant()}\n"
        f"date    : {date.today().isoformat()}\n"
        f"source  : https://github.com/M-CHADLI/mardik-api-mlops\n\n"
        f"Les fichiers de ce dossier sont des COPIES. En cas de divergence,\n"
        f"l'original dans le dépôt fait foi. Régénérer : python -m livrables.assembler\n",
        encoding="utf-8",
    )
    return comptes


if __name__ == "__main__":
    for dossier, n in assembler().items():
        print(f"{dossier:<26} {n} fichier(s)")
