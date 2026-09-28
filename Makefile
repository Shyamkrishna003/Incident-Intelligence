# Developer entry points. Python tooling runs from backend/ using the repo-local .venv.
# Paths are relative on purpose: the repository path may contain spaces.

.DEFAULT_GOAL := help
COMPOSE := docker compose
VENV_BIN := ../.venv/bin

.PHONY: help env venv lock db up down logs migrate run bootstrap test test-unit lint fmt typecheck check

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

db: ## Start PostgreSQL only (enough for migrations, the CLI, and tests)
	$(COMPOSE) up -d --wait postgres

up: ## Build and start PostgreSQL + API on http://localhost:8000
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (data volume is kept)
	$(COMPOSE) down

logs: ## Follow API logs
	$(COMPOSE) logs -f api

migrate: venv ## Apply database migrations
	cd backend && $(VENV_BIN)/alembic upgrade head

run: venv ## Run the API on the host with auto-reload
	cd backend && $(VENV_BIN)/uvicorn incident_intel.main:create_app --factory --reload --no-access-log

bootstrap: venv ## Create an org, project, and API key: make bootstrap ORG=acme PROJECT=payments
	@test -n "$(ORG)" -a -n "$(PROJECT)" || { echo "usage: make bootstrap ORG=<slug> PROJECT=<slug>"; exit 2; }
	cd backend && $(VENV_BIN)/ii bootstrap --org "$(ORG)" --project "$(PROJECT)"

test: venv ## Run all tests (integration tests need `make db`)
	cd backend && $(VENV_BIN)/pytest

test-unit: venv ## Run tests that need no database
	cd backend && $(VENV_BIN)/pytest -m "not integration"

lint: venv ## Lint and check formatting
	cd backend && $(VENV_BIN)/ruff check src tests && $(VENV_BIN)/ruff format --check src tests

fmt: venv ## Auto-format and apply safe lint fixes
	cd backend && $(VENV_BIN)/ruff format src tests && $(VENV_BIN)/ruff check --fix src tests

typecheck: venv ## Static type check (mypy --strict)
	cd backend && $(VENV_BIN)/mypy

check: lint typecheck test ## Lint, type check, and test
