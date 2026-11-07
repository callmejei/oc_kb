"""Local infrastructure is up, healthy and at the pinned versions. Requires `make up`."""

from __future__ import annotations

import importlib.util
import os

import pytest

from kb.config import REPO_ROOT, load_settings, read_dotenv

pytestmark = pytest.mark.integration

_spec = importlib.util.spec_from_file_location("kbinfra", REPO_ROOT / "infra" / "local" / "kbinfra.py")
assert _spec and _spec.loader
kbinfra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kbinfra)


@pytest.fixture(scope="module")
def env():
    e = read_dotenv(REPO_ROOT / ".env.local")
    if not e:
        pytest.fail(".env.local missing: run `make up` first")
    os.environ.update({k: v for k, v in e.items() if k.startswith("KB_")})
    return e


@pytest.fixture(scope="module")
def strict_settings(env):
    return load_settings("local", strict=True)


def test_postgres_up_and_pinned(strict_settings, env):
    ok, version = kbinfra.pg_health(strict_settings, env)
    assert ok and version.startswith("PostgreSQL 17.10")
    assert kbinfra.pg_psql(strict_settings, env, "show listen_addresses") == "127.0.0.1"


def test_postgres_database_exists(strict_settings, env):
    db = strict_settings.services["postgres"]["database"]
    assert kbinfra.pg_psql(strict_settings, env, "select current_database()", db=db) == db


def test_elasticsearch_up_pinned_and_secured(strict_settings, env):
    ok, health = kbinfra.es_health(strict_settings, env)
    assert ok, health
    _, info = kbinfra.http_ok(kbinfra._es_url(strict_settings), ("elastic", env["KB_ES_PASSWORD"]))
    assert info["version"]["number"] == kbinfra.ES_VERSION
    anon_ok, _ = kbinfra.http_ok(kbinfra._es_url(strict_settings) + "/_cluster/health")
    assert not anon_ok, "elasticsearch must reject anonymous requests"


def test_ollama_up_pinned_with_model(strict_settings, env):
    ok, v = kbinfra.ollama_health(strict_settings, env)
    assert ok and v["version"] == kbinfra.OLLAMA_VERSION
    _, tags = kbinfra.http_ok(strict_settings.services["ollama"]["url"] + "/api/tags")
    names = {m["name"] for m in tags["models"]}
    assert strict_settings.ports["llm"].options["model"] in names


def test_phoenix_up(strict_settings, env):
    ok, _ = kbinfra.phoenix_health(strict_settings, env)
    assert ok
