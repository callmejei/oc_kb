"""Migration 010: data-quality results are counts only, append-only, recorded only by the pipeline, retry-safe."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from psycopg.types.json import Jsonb


def payload() -> tuple[dict, list[dict]]:  # type: ignore[type-arg]
    run = {
        "run_id": str(uuid.uuid4()),
        "started_at": "2026-11-09T06:30:00+00:00",
        "finished_at": "2026-11-09T06:30:02+00:00",
        "as_of": "2026-11-09",
        "rules_sha256": "a" * 64,
    }
    results = [
        {"rule_id": "loan_id_unique", "table_name": "mart_credit.loan", "rule_kind": "unique",
         "column_name": "loan_id", "checked": 957, "failed": 0, "tolerance": 0, "passed": True},
        {"rule_id": "collateral_revalued_24m", "table_name": "mart_credit.collateral", "rule_kind": "max_age",
         "column_name": "valuation_date", "checked": 664, "failed": 41, "tolerance": 0.02, "passed": False},
    ]  # fmt: skip
    return run, results


def record(conn, run, results):  # type: ignore[no-untyped-def]
    return conn.execute("SELECT kb.record_dq_run(%s, %s) AS id", (Jsonb(run), Jsonb(results))).fetchone()["id"]


def test_pipeline_records_and_a_retry_is_a_noop(as_pipe):
    run, results = payload()
    with as_pipe() as c:
        assert str(record(c, run, results)) == run["run_id"]
    with as_pipe() as c:
        record(c, run, results)
        n = c.execute("SELECT count(*) AS n FROM kb.dq_result WHERE run_id = %s", (run["run_id"],)).fetchone()["n"]
    assert n == 2


def test_different_content_under_the_same_run_id_is_refused(as_pipe):
    run, results = payload()
    with as_pipe() as c:
        record(c, run, results)
    run["rules_sha256"] = "b" * 64
    with pytest.raises(pg.UniqueViolation, match="different content"), as_pipe() as c:
        record(c, run, results)


def test_the_app_role_cannot_record(as_api):
    run, results = payload()
    with pytest.raises(pg.InsufficientPrivilege), as_api("ret.user1") as c:
        record(c, run, results)


def test_results_are_append_only(as_pipe, as_admin):
    run, results = payload()
    with as_pipe() as c:
        record(c, run, results)
    with pytest.raises(pg.InsufficientPrivilege, match="append-only"), as_admin() as c:
        c.execute("UPDATE kb.dq_result SET failed = 0 WHERE run_id = %s", (run["run_id"],))
    with pytest.raises(pg.InsufficientPrivilege, match="append-only"), as_admin() as c:
        c.execute("DELETE FROM kb.dq_run WHERE run_id = %s", (run["run_id"],))


@pytest.mark.parametrize(("field", "value"), [("failed", 999), ("checked", -1), ("rule_kind", "sql"), ("tolerance", 2)])
def test_impossible_counts_and_kinds_are_refused(as_pipe, field, value):
    run, results = payload()
    results[0][field] = value
    with pytest.raises(pg.CheckViolation), as_pipe() as c:
        record(c, run, results)


def test_no_column_can_hold_row_values(as_admin):
    with as_admin() as c:
        cols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'kb' AND table_name = 'dq_result'"
        ).fetchall()}  # fmt: skip
    assert cols == {"run_id", "rule_id", "table_name", "rule_kind", "column_name", "checked", "failed",
                    "tolerance", "passed", "error"}  # fmt: skip
