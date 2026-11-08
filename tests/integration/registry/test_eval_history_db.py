"""Migration 009: evaluation history is append-only, written only by the pipeline role, retry-safe."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from psycopg.types.json import Jsonb


def payload(status: str = "PASS") -> tuple[dict, list[dict]]:
    run = {
        "run_id": str(uuid.uuid4()),
        "started_at": "2026-11-08T10:00:00+00:00",
        "finished_at": "2026-11-08T10:20:00+00:00",
        "git_commit": "abc1234",
        "status": status,
        "config": {"llm": "qwen3:8b"},
        "summary": {"leaks": 0},
        "report_uri": "docs/eval/report-x.json",
    }
    results = [
        {
            "bu": "retail",
            "suite": "golden",
            "metric": "answer_correct",
            "value": 0.9,
            "n": 10,
            "threshold": 0.8,
            "passed": True,
        },
        {"bu": "all", "suite": "leak", "metric": "leaks", "value": 0, "n": 68, "threshold": 0, "passed": True},
    ]
    return run, results


def record(conn, run, results):  # type: ignore[no-untyped-def]
    return conn.execute("SELECT kb.record_eval_run(%s, %s) AS id", (Jsonb(run), Jsonb(results))).fetchone()["id"]


def test_pipeline_records_a_run_and_a_retry_is_a_noop(as_pipe):
    run, results = payload()
    with as_pipe() as c:
        assert str(record(c, run, results)) == run["run_id"]
    with as_pipe() as c:
        assert str(record(c, run, results)) == run["run_id"]  # same content: no-op
        n = c.execute("SELECT count(*) AS n FROM kb.eval_result WHERE run_id = %s", (run["run_id"],)).fetchone()["n"]
        who = c.execute("SELECT recorded_by FROM kb.eval_run WHERE run_id = %s", (run["run_id"],)).fetchone()
    assert n == 2 and who["recorded_by"] == "svc:pipeline"


def test_a_different_payload_under_the_same_run_id_is_refused(as_pipe):
    run, results = payload()
    with as_pipe() as c:
        record(c, run, results)
    run["summary"] = {"leaks": 3}
    with pytest.raises(pg.UniqueViolation, match="different content"), as_pipe() as c:
        record(c, run, results)


def test_the_api_role_cannot_record_or_write_directly(as_api):
    run, results = payload()
    with pytest.raises(pg.InsufficientPrivilege), as_api("ret.checker") as c:
        record(c, run, results)
    with pytest.raises(pg.InsufficientPrivilege), as_api("ret.checker") as c:
        c.execute(
            "INSERT INTO kb.eval_run (run_id, started_at, finished_at, git_commit, status, config, summary,"
            " recorded_by)"
            " VALUES (gen_random_uuid(), now(), now(), 'x', 'PASS', '{}', '{}', 'me')"
        )


def test_history_is_append_only_even_for_a_superuser(as_pipe, as_admin):
    run, results = payload("FAIL")
    with as_pipe() as c:
        record(c, run, results)
    with pytest.raises(pg.InsufficientPrivilege, match="eval_result is append-only"), as_admin() as c:
        c.execute("UPDATE kb.eval_result SET value = 1 WHERE run_id = %s", (run["run_id"],))
    with pytest.raises(pg.InsufficientPrivilege, match="eval_run is append-only"), as_admin() as c:
        c.execute("DELETE FROM kb.eval_run WHERE run_id = %s", (run["run_id"],))


def test_bad_rows_are_rejected(as_pipe):
    run, results = payload()
    results[0]["suite"] = "vibes"
    with pytest.raises(pg.CheckViolation), as_pipe() as c:
        record(c, run, results)
    run2, _ = payload()
    with pytest.raises(pg.RaiseException, match="at least one result"), as_pipe() as c:
        record(c, run2, [])
