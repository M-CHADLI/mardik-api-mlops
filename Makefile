.PHONY: install up down serve proxy dashboard test test-integration test-acceptance eval traffic ci fixtures lint fmt clean

MODE ?= normal
VERSION ?= v2
DUREE ?= 60
RPS ?= 1

install:            ## dépendances (uv)
	uv sync

up:                 ## app + proxy de dérive + tableau de bord (docker compose)
	docker compose up -d --build
	@echo "app : http://localhost:8000/docs — proxy : http://localhost:8080/_drift — dashboard : http://localhost:8501"

down:
	docker compose down

serve:              ## app en local, sans docker (le proxy doit tourner : make proxy)
	uv run uvicorn app.main:app --reload --port 8000

proxy:              ## proxy de dérive en local
	uv run python -m ops.drift_proxy

dashboard:          ## tableau de bord en local (texte) — DASH=serve pour la page HTML
	uv run python -m ops.dashboard $(if $(filter serve,$(DASH)),--serve,)

test:               ## tout (intégration + acceptance), MOCK=on
	MOCK=on uv run pytest -q

test-integration:   ## hérités de la remédiation : verts
	MOCK=on uv run pytest -q tests/integration

test-acceptance:    ## les 10 tests du brief : 9 rouges, 1 vert au départ
	MOCK=on uv run pytest -v tests/acceptance

eval:               ## gate d'évaluation sur le VRAI modèle (VERSION=v2, ARGS="--essais 3")
	uv run python -m eval.run_eval --version $(VERSION) $(ARGS)

traffic:            ## trafic sur la gateway (MODE=normal|derive-score|derive-latence|erreurs)
	uv run python scripts/traffic_sim.py --mode $(MODE) --duree $(DUREE) --rps $(RPS)

fixtures:           ## (ré)enregistre les fixtures MOCK en appelant le vrai modèle
	MOCK=record uv run python -m eval.run_eval --version v1
	MOCK=record uv run python -m eval.run_eval --version $(VERSION)
	@echo "fixtures enregistrées dans eval/fixtures/ — à committer"

ci:                 ## l'équivalent local du workflow GitHub (MOCK=on)
	uv run ruff check .
	MOCK=on uv run pytest -q tests/integration
	MOCK=on uv run pytest -q tests/acceptance
	MOCK=on uv run python -m eval.run_eval --version v2 --seuil 0.75
	@echo "CI verte. Publication/canary : sur tag ou workflow_dispatch (voir .github/workflows/llmops.yml)"

lint:
	uv run ruff check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

clean:
	rm -rf .pytest_cache .ruff_cache ops/metrics.jsonl eval/history.jsonl eval/.metrics_eval.jsonl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
