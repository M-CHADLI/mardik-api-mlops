"""Registre d'artefacts local — [FOURNI].

Pas besoin d'un vrai registre : un dossier par version livrée, contenant le
bundle (``config.yaml``) et un ``manifest.json`` (commit, empreinte de la
config, note d'éval, date), plus un ``index.json`` qui dit quelle version
sert quel trafic. Suffisant pour étiqueter, promouvoir et revenir en arrière.

    ops/registry/
    ├── index.json          {"active": "v1.0.0", "canary": null, "canary_percent": 0}
    ├── journal.jsonl       [GÉNÉRÉ] événements de déploiement (une ligne JSON par événement)
    └── v1.0.0/
        ├── config.yaml     le bundle tel qu'il a été livré
        └── manifest.json   {"version", "commit", "empreinte", "note_eval", "date", ...}

Le registre ne décide rien : il enregistre. La logique de déploiement
(canary, promotion, rollback, surveillance) est dans ``ops/deploy.py``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.llm_client import Bundle

DOSSIER_REGISTRE = Path(__file__).resolve().parent
MOTIF_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


class ErreurRegistre(RuntimeError):
    pass


def _cle_tri(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.lstrip("v").split("."))


class Registry:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or os.environ.get("REGISTRY_PATH", DOSSIER_REGISTRE))
        self.root.mkdir(parents=True, exist_ok=True)
        if not self._chemin_index.exists():
            self.ecrire_index({"active": None, "canary": None, "canary_percent": 0})

    # ------------------------------------------------------------- artefacts
    def etiqueter(
        self,
        version: str,
        bundle: Bundle,
        *,
        commit: str,
        note_eval: float | None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dépose le bundle sous ``<root>/<version>/`` avec son manifeste.

        Une version déjà étiquetée est immuable : ré-étiqueter lève une erreur.
        """
        if not MOTIF_VERSION.match(version):
            raise ErreurRegistre(f"version invalide : {version!r} (attendu vX.Y.Z)")
        dossier = self.root / version
        if dossier.exists():
            raise ErreurRegistre(f"la version {version} est déjà étiquetée (immuable)")
        dossier.mkdir(parents=True)
        if bundle.chemin is None:
            raise ErreurRegistre("le bundle n'a pas de fichier config.yaml source")
        shutil.copy(bundle.chemin, dossier / "config.yaml")
        if "prompt_fichier" in bundle.chemin.read_text(encoding="utf-8"):
            for f in bundle.chemin.parent.iterdir():
                if f.suffix in {".txt", ".md"}:
                    shutil.copy(f, dossier / f.name)
        manifest = {
            "version": version,
            "commit": commit,
            "empreinte": bundle.empreinte(),
            "modele": bundle.modele,
            "strategie": bundle.strategie,
            "note_eval": note_eval,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **(details or {}),
        }
        (dossier / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    def versions(self) -> list[str]:
        return sorted(
            (d.name for d in self.root.iterdir() if d.is_dir() and MOTIF_VERSION.match(d.name)),
            key=_cle_tri,
        )

    def manifest(self, version: str) -> dict[str, Any]:
        chemin = self.root / version / "manifest.json"
        if not chemin.exists():
            raise ErreurRegistre(f"version inconnue du registre : {version}")
        return json.loads(chemin.read_text(encoding="utf-8"))

    def bundle(self, version: str) -> Bundle:
        return Bundle.charger_chemin(self.root / version / "config.yaml", version)

    # ----------------------------------------------------------------- index
    @property
    def _chemin_index(self) -> Path:
        return self.root / "index.json"

    def index(self) -> dict[str, Any]:
        return json.loads(self._chemin_index.read_text(encoding="utf-8"))

    def ecrire_index(self, index: dict[str, Any]) -> None:
        index = {**index, "mis_a_jour": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._chemin_index.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def active(self) -> str | None:
        return self.index().get("active")

    def canary(self) -> tuple[str | None, int]:
        idx = self.index()
        return idx.get("canary"), int(idx.get("canary_percent") or 0)

    def definir_actif(self, version: str) -> None:
        self.manifest(version)  # doit exister
        idx = self.index()
        idx["active"] = version
        self.ecrire_index(idx)

    def definir_canary(self, version: str | None, pourcentage: int) -> None:
        if version is not None:
            self.manifest(version)
        if not 0 <= pourcentage <= 100:
            raise ErreurRegistre("le pourcentage canary doit être entre 0 et 100")
        idx = self.index()
        idx["canary"] = version
        idx["canary_percent"] = pourcentage if version else 0
        self.ecrire_index(idx)

    # --------------------------------------------------------------- journal
    @property
    def _chemin_journal(self) -> Path:
        return self.root / "journal.jsonl"

    def journaliser(self, evenement: str, **details: Any) -> dict[str, Any]:
        entree = {
            "ts": time.time(),
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "evenement": evenement,
            **details,
        }
        with self._chemin_journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entree, ensure_ascii=False) + "\n")
        return entree

    def journal(self) -> list[dict[str, Any]]:
        if not self._chemin_journal.exists():
            return []
        return [
            json.loads(ligne)
            for ligne in self._chemin_journal.read_text(encoding="utf-8").splitlines()
            if ligne.strip()
        ]
