"""Assemble les livrables du brief dans `livrables/`, à partir des sources.

Le dossier `livrables/` est un **instantané** destiné au dépôt : il regroupe,
sous les cinq intitulés du brief, des copies des fichiers qui vivent ailleurs
dans le dépôt. Une copie diverge de sa source dès la première correction — d'où
ce script, qui la régénère au lieu de la rafistoler :

    python -m scripts.assembler_livrables

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
# Les CINQ livrables du brief, et rien d'autre. Ne sont copiés ici que les
# fichiers produits pour le brief : pas les fichiers fournis avec le squelette
# (api_v1, telemetry, registry, client_v1, besoin_client), pas les tests, pas
# l'outillage. Un dossier de remise qui contient autre chose que ce qui est
# demandé oblige le correcteur à trier.
LIVRABLES: dict[str, list[str]] = {
    # 1. Dossier de conception — besoin, contrat d'API, schéma de chaîne,
    #    schéma de boucle, tableau de pilotage.
    "1-dossier-de-conception": [
        # Le PDF est la version remise ; le Markdown reste la source, et c'est
        # lui qu'on corrige — `python -m scripts.exporter_pdf` régénère le PDF.
        "docs/dossier-de-conception.pdf",
        "docs/dossier-de-conception.md",
    ],
    # 2. L'API v2 du modèle : documents longs, score de confiance, erreurs
    #    explicites. (`app/api_v1.py` n'est pas copié : il est FOURNI et
    #    inchangé — c'est précisément ce qui prouve que la v1 est intacte.)
    "2-api-v2": [
        "app/api_v2.py",
        "app/pipeline/decoupage.py",
        "app/pipeline/extraction.py",
        "app/pipeline/consolidation.py",
        "app/pipeline/confiance.py",
        "app/gateway.py",
        "models/v2/config.yaml",
    ],
    # 3. La chaîne LLMOps : gates bloquants, artefacts étiquetés, canary.
    #    `ops/deploy.py` porte aussi les boucles 1 et 2 ; il est copié ici une
    #    seule fois, le livrable 4 y renvoie.
    "3-chaine-llmops": [
        ".github/workflows/llmops.yml",
        "eval/run_eval.py",
        "ops/deploy.py",
    ],
    # 4. L'observabilité : tableau de bord, trois boucles, journal de pilotage.
    "4-observabilite": [
        "ops/dashboard.py",
        "eval/enrichissement.py",
        "eval/anonymisation.py",
        # Le journal est un artefact d'exécution, pas du code : sans un exemple,
        # le livrable « journal de pilotage » ne serait qu'une promesse. Celui-ci
        # couvre un cycle complet — publication, canary, ajustement de seuil,
        # captures, promotion, enrichissement, rollback sur dérive.
        "docs/journal-de-pilotage-exemple.jsonl",
    ],
    # 5. Le client via un frontend accessible par un lien.
    "5-frontend": [
        "web/index.html",
        "web/tableau-de-bord.html",
        "web/commun.css",
        "app/frontend.py",
        "render.yaml",
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
        f"l'original dans le dépôt fait foi. Régénérer : python -m scripts.assembler_livrables\n",
        encoding="utf-8",
    )
    return comptes


if __name__ == "__main__":
    for dossier, n in assembler().items():
        print(f"{dossier:<26} {n} fichier(s)")
