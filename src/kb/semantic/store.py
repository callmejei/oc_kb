"""Published metrics from the registry, and running one as the end user (Phase 9c, D-067)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kb.semantic.model import Match, Metric, MetricError, compile_metric, validate
from kb.sql.policy import PolicyError


class MetricSource:
    """Approved metrics on ACTIVE products (view kb.metric_current), reloaded when kb.metric_version() moves.
    A definition that no longer validates against the catalogue is skipped (reported in ``errors``)."""

    def __init__(self, connect: Callable[[], Any], catalog: Mapping[str, Sequence[str]]) -> None:
        self._connect = connect
        self.catalog = catalog
        self._lock = threading.Lock()
        self._version: int | None = None
        self._metrics: list[Metric] = []
        self.errors: dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings: Any, engine: Any) -> MetricSource | None:
        opts = settings.ports["sql_engine"].options
        if opts.get("policy_source") != "registry" or not opts.get("policy_dsn"):
            return None  # metrics live in the registry; config-file mode has none
        import psycopg
        from psycopg.rows import dict_row

        dsn = str(opts["policy_dsn"])
        return cls(
            lambda: psycopg.connect(dsn, row_factory=dict_row, autocommit=True, connect_timeout=5),
            {t: tp.columns for t, tp in engine._policy.catalog.items()},
        )

    def current(self) -> list[Metric]:
        with self._connect() as con:
            v = int(con.execute("SELECT kb.metric_version() AS v").fetchone()["v"])
            with self._lock:
                if v != self._version:
                    rows = con.execute(
                        "SELECT metric_id, revision_no, definition, description FROM kb.metric_current"
                    ).fetchall()
                    out, errors = [], {}
                    for r in rows:
                        d = r["definition"]
                        try:
                            clean = validate(d, self.catalog.get(d.get("table", ""), ()))
                            if clean["columns"] != sorted(d.get("columns", [])):
                                raise MetricError("stored column list does not match the definition")
                            out.append(Metric(r["metric_id"], int(r["revision_no"]), clean, r["description"]))
                        except (MetricError, KeyError) as e:
                            errors[r["metric_id"]] = str(e)
                    self._metrics, self.errors, self._version = out, errors, v
                return list(self._metrics)


def run_metric(engine: Any, m: Match, identity: Any, row_limit: int) -> tuple[Any, Any, str] | None:
    """(SqlResult, Decision, compiled SQL) as the user, or None when the metric cannot serve this user (not granted,
    or a column it uses is masked for them): the caller then falls back to governed text-to-SQL."""
    d = m.metric.definition
    try:
        masked = engine.policy.masked_columns(identity, d["table"])
    except PolicyError:
        return None
    if set(d["columns"]) & set(masked):
        return None  # never compute a metric over masked values
    sql = compile_metric(d, m.group_by)
    try:
        res, dec = engine.execute_with_decision(sql, identity, row_limit=row_limit)
    except PolicyError:
        return None
    return res, dec, sql
