"""Traces + métriques — [FOURNI], hérité du brief de remédiation.

Trois primitives, déjà branchées sur ``/v1`` :

* un **tracer** OpenTelemetry (spans ``analyse.requete`` → ``llm.appel``) ;
* un **logger** structuré (structlog, JSON) ;
* un **journal de métriques** (``MetricsStore``) : une ligne JSON par requête
  servie — version, latence, erreur, score de confiance, coût, nombre d'appels
  LLM. C'est ce fichier que lisent le tableau de bord (``ops/dashboard.py``) et
  la surveillance du déploiement (``ops/deploy.py``).

Lire ``docs/schema_remediation.md`` pour savoir comment l'exploiter.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import structlog
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Tracer

RACINE = Path(__file__).resolve().parent.parent
CHEMIN_METRIQUES_DEFAUT = RACINE / "ops" / "metrics.jsonl"


def configure_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=False,
    )


@dataclass
class Mesure:
    """Une requête servie, telle qu'elle est journalisée."""

    ts: float
    version: str
    route: str
    latence_ms: float
    erreur: bool = False
    score: float | None = None
    cout_eur: float = 0.0
    appels_llm: int = 0
    tokens: int = 0
    tronque: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class MetricsStore:
    """Journal de métriques en JSONL, thread-safe, lisible à chaud."""

    def __init__(self, chemin: Path | str | None = None) -> None:
        self.chemin = Path(chemin or os.environ.get("METRICS_PATH", CHEMIN_METRIQUES_DEFAUT))
        self._verrou = threading.Lock()

    def enregistrer(self, mesure: Mesure) -> None:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        with self._verrou, self.chemin.open("a", encoding="utf-8") as f:
            f.write(json.dumps(mesure.to_dict(), ensure_ascii=False) + "\n")

    def lire(self, depuis_s: float | None = None, version: str | None = None) -> list[Mesure]:
        """Mesures des ``depuis_s`` dernières secondes (toutes si None)."""
        if not self.chemin.exists():
            return []
        seuil = time.time() - depuis_s if depuis_s else None
        mesures: list[Mesure] = []
        for ligne in self._lignes():
            m = Mesure(**ligne)
            if seuil is not None and m.ts < seuil:
                continue
            if version is not None and m.version != version:
                continue
            mesures.append(m)
        return mesures

    def purger(self) -> None:
        if self.chemin.exists():
            self.chemin.unlink()

    def _lignes(self) -> Iterator[dict[str, Any]]:
        with self._verrou, self.chemin.open(encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if ligne:
                    try:
                        yield json.loads(ligne)
                    except json.JSONDecodeError:
                        continue


@dataclass
class Telemetry:
    tracer: Tracer
    logger: Any
    metriques: MetricsStore
    spans: list[Any] = field(default_factory=list)


def build_telemetry(
    *,
    span_exporter: SpanExporter | None = None,
    metrics_path: Path | str | None = None,
    level: str = "INFO",
    service_name: str = "mardik",
) -> Telemetry:
    configure_logging(level)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(span_exporter or ConsoleSpanExporter()))
    tracer = provider.get_tracer(service_name)
    return Telemetry(
        tracer=tracer,
        logger=structlog.get_logger(service_name),
        metriques=MetricsStore(metrics_path),
    )


_defaut: Telemetry | None = None


def build_default_telemetry() -> Telemetry:
    """Télémétrie de production : exporte les spans sur la console (JSON),
    et les métriques dans ``ops/metrics.jsonl``."""
    global _defaut
    if _defaut is None:
        exporter: SpanExporter
        if os.environ.get("OTEL_TRACES", "console").lower() == "off":
            exporter = NoopSpanExporter()
        else:
            exporter = ConsoleSpanExporter()
        _defaut = build_telemetry(
            span_exporter=exporter,
            level=os.environ.get("LOG_LEVEL", "INFO"),
            service_name=os.environ.get("OTEL_SERVICE_NAME", "mardik"),
        )
    return _defaut


class NoopSpanExporter(SpanExporter):
    def export(self, spans):  # type: ignore[override]
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None
