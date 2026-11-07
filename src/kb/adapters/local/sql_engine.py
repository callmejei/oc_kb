"""DuckDbPolicySqlEngine: the local SqlEngine. Validated, policy-rewritten SELECTs on a hardened read-only
DuckDB connection (no external access, no extension autoload, configuration locked, capped resources)."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb

from kb.config import REPO_ROOT
from kb.ports.types import Identity, SqlResult
from kb.sql.policy import Decision, PolicyEngine, PolicyError, TablePolicy


class SqlTimeoutError(RuntimeError):
    pass


class DuckDbPolicySqlEngine:
    STATUS = "implemented"
    PHASE = 4

    def __init__(self, path: str, policies: str, marts: str = "config/marts.yaml", **_: Any) -> None:
        p = Path(path)
        self.path = p if p.is_absolute() else REPO_ROOT / p
        catalog = self._catalog()
        self.policy = PolicyEngine(policies, marts, catalog)
        self.last_decision: Decision | None = None
        self._con = self._connect()

    HARDENING = {
        "enable_external_access": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "allow_community_extensions": "false",
        "lock_configuration": "true",
    }

    def _connect(self) -> duckdb.DuckDBPyConnection:
        """One hardened connection per engine. DuckDB shares one database instance per file per process, and
        lock_configuration locks that instance: harden once, or verify it is already hardened."""
        con = duckdb.connect(str(self.path), read_only=True)
        current = dict(con.execute("SELECT name, value FROM duckdb_settings()").fetchall())
        if current.get("lock_configuration") != "true":
            con.execute(f"SET memory_limit = '{self.policy.cfg.get('memory_limit', '1GB')}'")
            con.execute(f"SET threads = {int(self.policy.cfg.get('threads', 2))}")
            for name, value in self.HARDENING.items():  # lock_configuration last (dict order)
                con.execute(f"SET {name} = {value}")
            current = dict(con.execute("SELECT name, value FROM duckdb_settings()").fetchall())
        bad = {k: current.get(k) for k, v in self.HARDENING.items() if current.get(k) != v}
        if bad:
            con.close()
            raise RuntimeError(f"DuckDB instance is not hardened: {bad}")
        return con

    def _catalog(self) -> dict[str, TablePolicy]:
        con = duckdb.connect(str(self.path), read_only=True)
        try:
            cols: dict[str, list[str]] = {}
            for s, t, c in con.execute(
                "SELECT table_schema, table_name, column_name FROM information_schema.columns "
                "WHERE table_schema LIKE 'mart_%' ORDER BY table_schema, table_name, ordinal_position"
            ).fetchall():
                cols.setdefault(f"{s}.{t}", []).append(c)
            return {
                t: TablePolicy(t, tuple(c), int(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]))  # type: ignore[index]  # noqa: S608
                for t, c in cols.items()
            }
        finally:
            con.close()

    # ---------------------------------------------------------------- port
    def permitted_schema(self, identity: Identity) -> Mapping[str, Sequence[str]]:
        return {t: list(self.policy.catalog[t].columns) for t in self.policy.granted_tables(identity)}

    def schema_context(self, identity: Identity) -> str:
        """Human/LLM-readable description of exactly the tables this user may query."""
        lines = []
        for t in self.policy.granted_tables(identity):
            meta = self.policy.descriptions.get(t, {})
            masked = self.policy.masked_columns(identity, t)
            lines.append(f"TABLE {t} -- {meta.get('description', '')}")
            for c in self.policy.catalog[t].columns:
                note = f" [MASKED: values are {masked[c]}ed for you]" if c in masked else ""
                lines.append(f"  {c}: {meta.get('columns', {}).get(c, '')}{note}")
        return "\n".join(lines)

    def execute(self, sql: str, identity: Identity, *, row_limit: int) -> SqlResult:
        result, decision = self.execute_with_decision(sql, identity, row_limit=row_limit)
        self.last_decision = decision  # convenience for single-threaded callers (tests); do not rely on it
        return result

    def execute_with_decision(self, sql: str, identity: Identity, *, row_limit: int) -> tuple[SqlResult, Decision]:
        """Thread-safe: the policy decision travels with its own result."""
        rewritten, decision = self.policy.prepare(sql, identity, row_limit)
        con = self._con.cursor()  # shares the hardened instance and its locked settings
        timer = threading.Timer(float(self.policy.cfg.get("timeout_s", 10)), con.interrupt)
        timer.start()
        try:
            cur = con.execute(rewritten)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
        except duckdb.InterruptException:
            raise SqlTimeoutError("query exceeded the time limit") from None
        except duckdb.Error as e:
            raise PolicyError("execution_error", str(e).splitlines()[0]) from None
        finally:
            timer.cancel()
            con.close()
        truncated = len(rows) > decision.limit
        executed = rewritten + ("  -- truncated" if truncated else "")
        return SqlResult(
            columns=columns, rows=rows[: decision.limit], executed_sql=executed, tables=decision.tables
        ), decision
