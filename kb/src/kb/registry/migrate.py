"""Migration runner: ordered plain-SQL files, one transaction each, checksummed, idempotent.

- ``000_*`` runs as the admin (cluster roles, database grants).
- Every later file runs under ``SET LOCAL ROLE kb_owner`` so all objects, including the
  SECURITY DEFINER governance functions, are owned by a non-superuser role.
- An applied file whose checksum changed is refused (write a new migration instead).
- Login-role passwords come from the environment and are (re)set on every run.

Usage: python -m kb.registry.migrate [--database NAME]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from importlib import resources
from typing import Any

import psycopg
from psycopg import sql

from kb.config import REPO_ROOT, load_settings, read_dotenv

OWNER_ROLE = "kb_owner"
LOGIN_PASSWORD_VARS = {"kb_api": "KB_PG_API_PASSWORD", "kb_pipe": "KB_PG_PIPELINE_PASSWORD"}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover() -> list[Migration]:
    pkg = resources.files("kb.registry") / "migrations"
    out = []
    for f in sorted(pkg.iterdir(), key=lambda p: p.name):
        if f.name.endswith(".sql"):
            version, _, rest = f.name.partition("_")
            out.append(Migration(version, rest.removesuffix(".sql"), f.read_text(encoding="utf-8")))
    versions = [m.version for m in out]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions: {versions}")
    return out


def admin_conninfo(database: str | None = None) -> dict[str, Any]:
    env = {**read_dotenv(REPO_ROOT / ".env.local"), **os.environ}
    pg = load_settings("local").services["postgres"]
    return {
        "host": pg["host"],
        "port": pg["port"],
        "user": pg["admin_user"],
        "password": env["KB_PG_ADMIN_PASSWORD"],
        "dbname": database or pg["database"],
    }


def _ensure_meta(conn: psycopg.Connection[Any]) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS kb_meta")
    conn.execute("REVOKE ALL ON SCHEMA kb_meta FROM PUBLIC")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kb_meta.schema_migration (
             version text PRIMARY KEY, name text NOT NULL, checksum char(64) NOT NULL,
             applied_at timestamptz NOT NULL DEFAULT now(), applied_by text NOT NULL DEFAULT session_user)"""
    )


def migrate(conninfo: dict[str, Any], *, log: Any = print) -> list[str]:
    """Apply pending migrations. Returns the versions applied in this run."""
    applied_now: list[str] = []
    with psycopg.connect(**conninfo, autocommit=True) as conn:
        _ensure_meta(conn)
        done = {v: c for v, c in conn.execute("SELECT version, checksum FROM kb_meta.schema_migration")}
        for m in discover():
            if m.version in done:
                if done[m.version].strip() != m.checksum:
                    raise MigrationError(f"migration {m.version}_{m.name} changed after it was applied")
                continue
            with conn.transaction():
                if m.version != "000":
                    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER_ROLE)))
                conn.execute(m.sql)  # no params: simple protocol, multi-statement OK
                conn.execute("RESET ROLE")
                conn.execute(
                    "INSERT INTO kb_meta.schema_migration (version, name, checksum) VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
            applied_now.append(m.version)
            log(f"applied {m.version}_{m.name}")
        _set_login_passwords(conn)
    return applied_now


def _set_login_passwords(conn: psycopg.Connection[Any]) -> None:
    env = {**read_dotenv(REPO_ROOT / ".env.local"), **os.environ}
    for role, var in LOGIN_PASSWORD_VARS.items():
        if env.get(var):
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(env[var])))


def create_database(name: str, *, drop: bool = False) -> None:
    info = admin_conninfo("postgres")
    with psycopg.connect(**info, autocommit=True) as conn:
        if drop:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.registry.migrate")
    ap.add_argument("--database")
    a = ap.parse_args(argv)
    applied = migrate(admin_conninfo(a.database))
    print(f"migrations up to date ({len(applied)} applied now)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
