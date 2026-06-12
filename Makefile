# Nidria backend — daily dev commands. Everything goes through `uv run`
# (no globally installed tool assumed). `make help` lists the targets.

.DEFAULT_GOAL := help

# Same paths as ci.yml so `make check` green == CI green.
LINT_PATHS := src/ shared/ tests/ scripts/ alembic/env.py

.PHONY: help dev dev-scheduler openapi db-upgrade db-downgrade db-current \
	db-history db-migration db-reset seed lint format format-check \
	typecheck test test-cov check version release

# --- Help ----------------------------------------------------------------

help: ## List targets with their one-line description
	@grep -E '^[a-zA-Z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- API -----------------------------------------------------------------

dev: ## Run the API on :8001 with reload (scheduler OFF: set SCHEDULER_ENABLED=false in .env)
	uv run uvicorn src.main:app --reload --port 8001

dev-scheduler: ## Run the API on :8001 with reload AND the scheduler ON
	SCHEDULER_ENABLED=true uv run uvicorn src.main:app --reload --port 8001

openapi: ## Regenerate the committed openapi.json (contract-first)
	uv run python scripts/export_openapi.py

# --- Database ------------------------------------------------------------

db-upgrade: ## Apply migrations up to head
	uv run alembic upgrade head

db-downgrade: ## Roll back the last migration
	uv run alembic downgrade -1

db-current: ## Show the current migration revision
	uv run alembic current

db-history: ## Show the migration history
	uv run alembic history

db-migration: ## Create an autogenerate migration: make db-migration name="add_x"
	@if [ -z "$(name)" ]; then \
		echo 'ERROR: name is required — make db-migration name="add_x"'; exit 1; \
	fi
	uv run alembic revision --autogenerate -m "$(name)"

db-reset: ## DANGER: drop + recreate the LOCAL DEV database, upgrade head, seed
	@uv run python -c "import sys; \
		from src.core.config import get_settings; \
		env = get_settings().environment; \
		sys.exit(0) if env in ('development', 'local') else \
		sys.exit(f'Refusing db-reset: ENVIRONMENT={env!r} (need development|local)')"
	@printf "This will DROP the local dev database schema. Type 'yes' to continue: "; \
	read answer; \
	if [ "$$answer" != "yes" ]; then echo "Aborted."; exit 1; fi
	@uv run python -c "from sqlalchemy import create_engine, text; \
		from src.core.config import get_settings; \
		engine = create_engine(get_settings().database_url_sync, isolation_level='AUTOCOMMIT'); \
		conn = engine.connect(); \
		conn.execute(text('DROP SCHEMA public CASCADE')); \
		conn.execute(text('CREATE SCHEMA public')); \
		conn.close(); \
		print('Schema dropped and recreated.')"
	uv run alembic upgrade head
	uv run python scripts/seed.py

# --- Seed ----------------------------------------------------------------

seed: ## Seed demo data + RBAC baseline (idempotent: get-or-create, safe to re-run)
	uv run python scripts/seed.py

# --- Quality (same commands as ci.yml) -------------------------------------

lint: ## ruff check (same paths as CI)
	uv run ruff check $(LINT_PATHS)

format: ## ruff format (writes files)
	uv run ruff format $(LINT_PATHS)

format-check: ## ruff format --check (CI mode, no writes)
	uv run ruff format --check $(LINT_PATHS)

typecheck: ## mypy strict on src/ shared/ (same as CI)
	uv run mypy src/ shared/

test: ## Full test suite (testcontainers PG, parallel)
	uv run pytest tests/ -x -q -n auto

test-cov: ## Test suite with coverage on src/
	uv run pytest tests/ -q -n auto --cov=src

check: lint format-check typecheck test ## The full pre-push gate in one command
	@echo "All checks passed — CI should be green."

# --- Versioning (semver) ----------------------------------------------------

version: ## Show current version (latest tag + pyproject, flags drift)
	@TAG=$$(git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0); \
	PY=$$(grep -m1 '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/'); \
	echo "tag:       $$TAG"; \
	echo "pyproject: $$PY"; \
	if [ "$${TAG#v}" != "$$PY" ]; then \
		echo "WARNING: tag and pyproject.toml version are out of sync"; \
	fi

release: ## Cut a release: make release type=patch|minor|major (clean main only)
	@if [ "$(type)" != "patch" ] && [ "$(type)" != "minor" ] && [ "$(type)" != "major" ]; then \
		echo 'ERROR: make release type=patch|minor|major'; exit 1; \
	fi
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "ERROR: working tree not clean — commit or stash first."; exit 1; \
	fi
	@if [ "$$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then \
		echo "ERROR: releases are cut from main."; exit 1; \
	fi
	@LAST=$$(git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0); \
	VER=$${LAST#v}; \
	MAJOR=$${VER%%.*}; REST=$${VER#*.}; MINOR=$${REST%%.*}; PATCH=$${REST#*.}; \
	case "$(type)" in \
		major) MAJOR=$$((MAJOR+1)); MINOR=0; PATCH=0;; \
		minor) MINOR=$$((MINOR+1)); PATCH=0;; \
		patch) PATCH=$$((PATCH+1));; \
	esac; \
	NEW="$$MAJOR.$$MINOR.$$PATCH"; \
	echo "Releasing v$$NEW (from $$LAST)..."; \
	sed -i.bak "s/^version = \".*\"/version = \"$$NEW\"/" pyproject.toml && rm pyproject.toml.bak; \
	uv lock; \
	git add pyproject.toml uv.lock; \
	git commit -m "chore(release): v$$NEW"; \
	git tag -a "v$$NEW" -m "v$$NEW"; \
	git push && git push --tags; \
	echo "Released v$$NEW — the CI deploy will embed it via APP_VERSION."
