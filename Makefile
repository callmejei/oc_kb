# Thin wrappers; logic lives in infra/local/kbinfra.py and synthetic/. On Windows use .\kb.ps1 <target>.
UV ?= uv
PY := $(UV) run python

.PHONY: help sync fetch up down status models seed migrate onboard api test lint typecheck unit integration

help:
	@echo "targets: sync fetch up down status models seed migrate onboard api test lint typecheck unit integration"

sync:
	$(UV) sync --all-groups --locked

fetch: sync
	$(PY) infra/local/kbinfra.py fetch

up: sync
	$(PY) infra/local/kbinfra.py up

down:
	$(PY) infra/local/kbinfra.py down

status:
	$(PY) infra/local/kbinfra.py status

models:
	$(PY) infra/local/kbinfra.py models

seed:
	$(PY) -m synthetic seed

migrate:
	$(PY) -m kb.registry.migrate
	$(PY) -m kb.registry.rolesync

onboard:
	$(PY) -m kb.ingest.bootstrap --wait

api:
	$(UV) run uvicorn kb.gateway.app:create_app --factory --host 127.0.0.1 --port 8000

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck:
	$(UV) run mypy

unit:
	$(UV) run pytest -m "not integration"

integration:
	$(UV) run pytest -m integration

test: lint typecheck
	$(UV) run pytest
