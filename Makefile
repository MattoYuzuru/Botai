# BotAI developer commands. `make check` is the gate for every PR (see AGENTS.md).
SHELL := /bin/bash
BACKEND := backend
export DATABASE_URL ?= postgresql+asyncpg://botai:botai@127.0.0.1:55432/botai

.PHONY: setup db db-stop gen migrate check check-backend check-web check-gen test-backend test-web dev-api dev-web dev-worker bench

setup:            ## install backend and frontend dependencies
	cd $(BACKEND) && uv sync --frozen
	pnpm install --frozen-lockfile

db:               ## start the shared dev Postgres (idempotent; never down -v)
	docker compose -f ops/compose.dev.yaml up -d --wait

db-stop:
	docker compose -f ops/compose.dev.yaml stop

gen:              ## regenerate contracts copies, OpenAPI and TS types
	./scripts/gen.sh

migrate:
	cd $(BACKEND) && uv run --frozen alembic upgrade head

check: check-backend check-web check-gen   ## full gate

check-backend:
	cd $(BACKEND) && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen pyright && uv run --frozen pytest -q

check-web:
	pnpm exec biome check . && pnpm -r typecheck && pnpm -r test

check-gen:        ## fails if generated files are stale
	./scripts/gen.sh && git diff --exit-code -- backend/openapi.json backend/src/botai/contracts/schemas apps/web/src/api

test-backend:
	cd $(BACKEND) && uv run --frozen pytest -q

test-web:
	pnpm -r test

dev-api:
	cd $(BACKEND) && uv run --frozen uvicorn botai.main:app --reload --port 8000

dev-worker:
	cd $(BACKEND) && uv run --frozen python -m botai.worker

dev-web:
	pnpm --filter @botai/web dev

SCENARIO ?= B4
bench:            ## agent benchmark (L2), e.g. `make bench SCENARIO=B1 ARGS="--models flash --profiles low,high"`
	cd $(BACKEND) && BOTAI_LIVE_LLM=1 uv run --frozen python -m botai.agent.bench --scenario $(SCENARIO) $(ARGS)
