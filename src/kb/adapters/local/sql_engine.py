"""DuckDbPolicySqlEngine: the local SqlEngine. Validated, policy-rewritten SELECTs on a hardened read-only
DuckDB connection (no external access, no extension autoload, configuration locked, capped resources)."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb
import psycopg
from psycopg.rows import dict_row

from kb.config import REPO_ROOT
from kb.ports.types import Identity, SqlResult
from kb.sql.policy import Decision, PolicyEngine, PolicyError, TablePolicy
from kb.sql.policy_store import RegistryPolicySource


class SqlTimeoutError(RuntimeError):
    pass


class DuckDbPolicySqlEngine:
    STATUS = "implemented"
    PHASE = 4

    def __init__(
        self,
        path: str,
        policies: str = "config/policies.yaml",
        marts: str = "config/marts.yaml",
        policy_source: str = "yaml",
        policy_dsn: str | None = None,
        **_: Any,
    ) -> None:
        """policy_source: "yaml" (config file; unit tests) or "registry" (D-066: approved data products, read on
        every use through a cheap version check, so a restriction applies to the next query)."""
        p = Path(path)
        self.path = p if p.is_absolute() else REPO_ROOT / p
        catalog = self._catalog()
        self._lock = threading.RLock()
        self._source: RegistryPolicySource | None = None
        if policy_source == "registry":
            if not policy_dsn:
                raise ValueError("policy_source=registry needs policy_dsn")
            dsn = policy_dsn
            self._source = RegistryPolicySource(
                lambda: psycopg.connect(dsn, row_factory=dict_row, autocommit=True, connect_timeout=5),
                {t: tp.columns for t, tp in catalog.items()},
            )
            self._policy = PolicyEngine({}, marts, catalog)  # deny all until the first load
        elif policy_source == "yaml":
            self._policy = PolicyEngine(policies, marts, catalog)
        else:
            raise ValueError(f"unknown policy_source {policy_source!r}")
        self.policy_errors: dict[str, str] = {}
        self.last_decision: Decision | None = None
        self._con = self._connect()

    @property
    def policy(self) -> PolicyEngine:
        """The policy in force now. Registry mode: reloads when kb.policy_version() moved; if the registry cannot be
        read, refuses (never serves a possibly revoked policy)."""
        if self._source is None:
            return self._policy
        with self._lock:
            try:
                loaded = self._source.current()
            except Exception as e:  # noqa: BLE001 - any failure to read the policy fails closed
                raise PolicyError("policy_unavailable", f"access policy cannot be read: {type(e).__name__}") from None
            if self._policy.version != loaded.version:
                self._policy.set_policies(loaded.policies, loaded.version)
                self.policy_errors = loaded.errors
            return self._policy

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
            con.execute(f"SET memory_limit = '{self._policy.cfg.get('memory_limit', '1GB')}'")
            con.execute(f"SET threads = {int(self._policy.cfg.get('threads', 2))}")
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

    # ---------------------------------------------------------------- pipeline
    def check_counts(self, sql: str) -> tuple[int, int]:
        """Data-quality measurement as the PIPELINE (no user policy: quality is measured on every row). Accepts
        only one SELECT and returns only one row of two non-negative integers (checked, failed), so no value
        or row id can come back through it. Callers: kb.quality.rules (rules compiled from config), never
        user text. On CML this runs under the pipeline service account on Starburst."""
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse(sql, read="duckdb")
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
            raise PolicyError("not_select", "quality checks must be one SELECT")
        con = self._con.cursor()
        timer = threading.Timer(float(self._policy.cfg.get("timeout_s", 10)), con.interrupt)
        timer.start()
        try:
            rows = con.execute(sql).fetchall()
        except duckdb.InterruptException:
            raise SqlTimeoutError("quality check exceeded the time limit") from None
        finally:
            timer.cancel()
            con.close()
        if len(rows) != 1 or len(rows[0]) != 2 or not all(isinstance(v, int) and v >= 0 for v in rows[0]):
            raise PolicyError("bad_shape", "a quality check must return one row of two counts")
        return int(rows[0][0]), int(rows[0][1])

    # ---------------------------------------------------------------- port
    def permitted_schema(self, identity: Identity) -> Mapping[str, Sequence[str]]:
        pol = self.policy
        return {t: list(pol.catalog[t].columns) for t in pol.granted_tables(identity)}

    def schema_context(self, identity: Identity) -> str:
        """Human/LLM-readable description of exactly the tables this user may query."""
        lines = []
        with self._lock:  # one consistent policy for the whole description
            pol = self.policy
            tables = [(t, pol.masked_columns(identity, t)) for t in pol.granted_tables(identity)]
        for t, masked in tables:
            meta = pol.descriptions.get(t, {})
            lines.append(f"TABLE {t} -- {meta.get('description', '')}")
            for c in pol.catalog[t].columns:
                note = f" [MASKED: values are {masked[c]}ed for you]" if c in masked else ""
                lines.append(f"  {c}: {meta.get('columns', {}).get(c, '')}{note}")
        return "\n".join(lines)

    def execute(self, sql: str, identity: Identity, *, row_limit: int) -> SqlResult:
        result, decision = self.execute_with_decision(sql, identity, row_limit=row_limit)
        self.last_decision = decision  # convenience for single-threaded callers (tests); do not rely on it
        return result

    def execute_with_decision(self, sql: str, identity: Identity, *, row_limit: int) -> tuple[SqlResult, Decision]:
        """Thread-safe: the policy decision travels with its own result."""
        with self._lock:  # the rewrite sees one policy version, never a half-swapped one
            pol = self.policy
            rewritten, decision = pol.prepare(sql, identity, row_limit)
        con = self._con.cursor()  # shares the hardened instance and its locked settings
        timer = threading.Timer(float(pol.cfg.get("timeout_s", 10)), con.interrupt)
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
