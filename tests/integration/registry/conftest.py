"""Registry fixtures: a throwaway kb_test database, migrated and role-synced once per session."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from kb.adapters.local.identity import YamlIdentityProvider
from kb.adapters.local.registry import PostgresRegistry
from kb.config import REPO_ROOT, load_settings, read_dotenv
from kb.gateway.app import create_app
from kb.registry import migrate, rolesync

from helpers import Api


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = __file__.rsplit("conftest", 1)[0]
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.integration)


TEST_DB = "kb_test"


@pytest.fixture(scope="session")
def secrets() -> dict[str, str]:
    env = read_dotenv(REPO_ROOT / ".env.local")
    if not env:
        pytest.fail(".env.local missing: run `make up` first")
    os.environ.update({k: v for k, v in env.items() if k.startswith("KB_")})
    return env


@pytest.fixture(scope="session")
def regdb(secrets) -> dict[str, Any]:
    migrate.create_database(TEST_DB, drop=True)
    info = migrate.admin_conninfo(TEST_DB)
    migrate.migrate(info, log=lambda *_: None)
    rolesync.sync(info, YamlIdentityProvider("config/users.yaml"))
    return info


def _dsn(user: str, password: str) -> str:
    return f"postgresql://{user}:{password}@127.0.0.1:5433/{TEST_DB}"


@pytest.fixture(scope="session")
def api_dsn(regdb, secrets) -> str:
    return _dsn("kb_api", secrets["KB_PG_API_PASSWORD"])


@pytest.fixture(scope="session")
def pipe_dsn(regdb, secrets) -> str:
    return _dsn("kb_pipe", secrets["KB_PG_PIPELINE_PASSWORD"])


@contextmanager
def _connect(dsn_or_info: str | dict[str, Any], actor: str | None) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    kw = {"conninfo": dsn_or_info} if isinstance(dsn_or_info, str) else dict(dsn_or_info)
    with psycopg.connect(**kw, autocommit=True, row_factory=dict_row) as conn:
        if actor:
            conn.execute("SELECT set_config('kb.actor_id', %s, false)", (actor,))
        yield conn


@pytest.fixture()
def as_api(api_dsn) -> Callable[..., Any]:
    """Direct SQL as the API login (bypasses the HTTP layer entirely)."""
    return lambda actor=None: _connect(api_dsn, actor)


@pytest.fixture()
def as_pipe(pipe_dsn) -> Callable[..., Any]:
    return lambda actor="svc:pipeline": _connect(pipe_dsn, actor)


@pytest.fixture()
def as_admin(regdb) -> Callable[..., Any]:
    """Superuser connection (break-glass); used to prove triggers hold even for it."""
    return lambda actor="test:admin": _connect(regdb, actor)


@pytest.fixture(scope="session")
def registry(api_dsn) -> Iterator[PostgresRegistry]:
    reg = PostgresRegistry(api_dsn)
    yield reg
    reg.close()


@pytest.fixture(scope="session")
def client(registry) -> Iterator[TestClient]:
    app = create_app(
        settings=load_settings("local"),
        registry=registry,
        idp=YamlIdentityProvider("config/users.yaml"),
        trusted_clients=frozenset({"testclient"}),  # starlette TestClient's client host
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def api(client) -> Api:
    return Api(client)
