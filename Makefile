# Developer entry points. Python tooling runs from backend/ using the repo-local .venv.
# Paths are relative on purpose: the repository path may contain spaces.

.DEFAULT_GOAL := help
COMPOSE := docker compose
VENV_BIN := ../.venv/bin

.PHONY: help env venv lock db infra emulator kafka-init up down logs migrate run consume dlq bootstrap test test-unit lint fmt typecheck check

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

env: ## Create .env from .env.example with a generated API-key pepper (never overwrites)
	@if [ -f .env ]; then echo ".env already exists; left untouched"; else \
	  cp .env.example .env && \
	  sed -i "s|^API_KEY_PEPPER=.*|API_KEY_PEPPER=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" .env && \
	  echo "Created .env"; fi

venv: .venv/.installed ## Create .venv and install the locked dependencies

.venv/.installed: backend/requirements-dev.lock backend/pyproject.toml
	python3 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet --require-hashes -r backend/requirements-dev.lock
	.venv/bin/pip install --quiet --no-deps -e backend
	touch $@

lock: ## Re-resolve pinned dependencies after editing backend/pyproject.toml
	cd backend && $(VENV_BIN)/pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe -o requirements.lock pyproject.toml
	cd backend && $(VENV_BIN)/pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe --extra dev -o requirements-dev.lock pyproject.toml

db: ## Start PostgreSQL only
	$(COMPOSE) up -d --wait postgres

infra: venv ## Start PostgreSQL + Kafka + Redis, create topics (enough for host-run API and tests)
	$(COMPOSE) up -d --wait postgres kafka redis
	cd backend && $(VENV_BIN)/ii kafka init

emulator: ## Start the Firebase Auth emulator on localhost:9099 (optional; large first build)
	$(COMPOSE) --profile emulator up -d --build --wait firebase-emulator

kafka-init: venv ## Create missing Kafka topics
	cd backend && $(VENV_BIN)/ii kafka init

up: ## Build and start everything in Docker (API on http://localhost:8000)
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) down

logs: ## Follow API and storage-consumer logs
	$(COMPOSE) logs -f api storage-consumer

migrate: venv ## Apply database migrations
	cd backend && $(VENV_BIN)/alembic upgrade head

run: venv ## Run the API on the host with auto-reload
	cd backend && $(VENV_BIN)/uvicorn incident_intel.main:create_app --factory --reload --no-access-log

consume: venv ## Run the storage consumer on the host (Kafka -> PostgreSQL)
	cd backend && $(VENV_BIN)/ii consume storage

dlq: venv ## Show dead-lettered messages (metadata only)
	cd backend && $(VENV_BIN)/ii dlq inspect

bootstrap: venv ## Create an org, project, and API key: make bootstrap ORG=acme PROJECT=payments
	@test -n "$(ORG)" -a -n "$(PROJECT)" || { echo "usage: make bootstrap ORG=<slug> PROJECT=<slug>"; exit 2; }
	cd backend && $(VENV_BIN)/ii bootstrap --org "$(ORG)" --project "$(PROJECT)"

test: venv ## Run all tests (needs `make infra`)
	cd backend && $(VENV_BIN)/pytest

test-unit: venv ## Run tests that need no PostgreSQL, Kafka, or Redis
	cd backend && $(VENV_BIN)/pytest -m "not integration and not kafka and not redis and not firebase"

lint: venv ## Lint and check formatting
	cd backend && $(VENV_BIN)/ruff check src tests && $(VENV_BIN)/ruff format --check src tests

fmt: venv ## Auto-format and apply safe lint fixes
	cd backend && $(VENV_BIN)/ruff format src tests && $(VENV_BIN)/ruff check --fix src tests

typecheck: venv ## Static type check (mypy --strict)
	cd backend && $(VENV_BIN)/mypy

check: lint typecheck test ## Lint, type check, and test
