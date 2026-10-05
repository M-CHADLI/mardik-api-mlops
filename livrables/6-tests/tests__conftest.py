"""Fixtures partagées — [FOURNI].

Tous les tests tournent en ``MOCK=on`` (aucun appel réseau) avec un registre,
un journal de métriques et un historique d'éval isolés dans ``tmp_path``.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import api_v1, api_v2, gateway
from app.llm_client import Bundle
from app.main import create_app
from app.telemetry import MetricsStore, Telemetry, build_telemetry
from ops.registry import Registry

RACINE = Path(__file__).resolve().parent.parent
CONTRATS = RACINE / "eval" / "contrats"


@pytest.fixture(autouse=True)
def environnement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MOCK", "on")
    monkeypatch.setenv("DRIFT", "off")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "modele-de-test")
    monkeypatch.delenv("CANARY_PERCENT", raising=False)
    monkeypatch.setenv("METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "registry"))
    monkeypatch.setenv("OTEL_TRACES", "off")
    return tmp_path


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    """Registre isolé, avec la v1.0.0 déjà livrée et active (l'état de départ)."""
    reg = Registry(tmp_path / "registry")
    if "v1.0.0" not in reg.versions():
        reg.etiqueter("v1.0.0", Bundle.charger("v1"), commit="historique", note_eval=None)
        reg.definir_actif("v1.0.0")
    return reg


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def metriques(tmp_path: Path) -> MetricsStore:
    return MetricsStore(tmp_path / "metrics.jsonl")


@pytest.fixture
def telemetry(span_exporter: InMemorySpanExporter, tmp_path: Path) -> Telemetry:
    return build_telemetry(
        span_exporter=span_exporter, metrics_path=tmp_path / "metrics.jsonl", level="INFO"
    )


@pytest.fixture
def client(telemetry: Telemetry, registry: Registry) -> TestClient:
    app = create_app()
    for module in (api_v1, api_v2, gateway):
        app.dependency_overrides[module.get_telemetry] = lambda: telemetry
    app.dependency_overrides[gateway.get_registry] = lambda: registry
    return TestClient(app)


@pytest.fixture
def contrat():
    def _lire(cid: str) -> str:
        return (CONTRATS / f"{cid}.txt").read_text(encoding="utf-8")

    return _lire


@pytest.fixture
def historique(tmp_path: Path) -> Path:
    return tmp_path / "history.jsonl"


@pytest.fixture
def contrats_courts(tmp_path: Path) -> Path:
    """Sous-ensemble de contrats (les 4 premiers) pour garder les tests rapides."""
    dossier = tmp_path / "contrats"
    dossier.mkdir()
    for cid in ("c01", "c02", "c03", "c04"):
        shutil.copy(CONTRATS / f"{cid}.txt", dossier / f"{cid}.txt")
    return dossier
