"""Phase 7 operations dashboard API: ops group only, counts and metadata only, ops grants no content access."""

from __future__ import annotations

import json
import uuid

import pytest
from psycopg.types.json import Jsonb

from kb.adapters.local.identity import YamlIdentityProvider

from helpers import onboard

OPS = "ops.analyst"
ENDPOINTS = ("/ops/ingestion", "/ops/exceptions", "/ops/eval", "/ops/feedback")


def get(client, path: str, user: str):  # type: ignore[no-untyped-def]
    return client.get("/api/v1" + path, headers={"X-KB-User": user})


@pytest.mark.parametrize("user", ["ret.user1", "ret.checker", "data.steward", "nobody"])
@pytest.mark.parametrize("path", ENDPOINTS)
def test_non_ops_users_are_refused(client, path, user):
    r = get(client, path, user)
    assert r.status_code == 403, (path, user, r.text)


def test_ops_membership_grants_no_document_access():
    idp = YamlIdentityProvider("config/users.yaml")
    ident = idp.resolve(OPS)
    assert "GRP_KB_OPS" in ident.groups and idp.readable_bus(ident) == set() and idp.governance_roles(ident) == set()


def test_me_reports_ops_flag(client):
    assert get(client, "/me", OPS).json()["ops"] is True
    assert get(client, "/me", "ret.user1").json()["ops"] is False


def test_ingestion_and_exceptions_show_counts_for_every_bu(client, api, as_pipe):
    src = onboard(api, "wealth")["source_id"]
    with as_pipe() as c:
        c.execute(
            "INSERT INTO kb.exception_item (source_id, reason, detail) VALUES (%s, 'PARSE_FAILURE', %s)",
            (src, Jsonb({"message": "synthetic"})),
        )
    rows = get(client, "/ops/ingestion", OPS).json()
    mine = next(r for r in rows if r["source_id"] == src)
    assert mine["bu"] == "wealth" and mine["open_exceptions"] >= 1 and "indexed_docs" in mine
    exc = get(client, "/ops/exceptions", OPS).json()
    assert exc["total_open"] >= 1
    assert any(r["bu"] == "wealth" and r["reason"] == "PARSE_FAILURE" for r in exc["by_bu_reason"])


def test_eval_trend_and_leak_status_come_from_the_recorded_runs(client, as_pipe):
    run_id = str(uuid.uuid4())
    run = {"run_id": run_id, "started_at": "2030-01-01T00:00:00+00:00", "finished_at": "2030-01-01T00:30:00+00:00",
           "git_commit": "test123", "status": "PASS", "config": {"llm": "qwen3:8b", "judge": "llama3.1:8b"},
           "summary": {"failures": []}, "report_uri": "docs/eval/report-test.json"}  # fmt: skip
    results = [
        {"bu": "all", "suite": "golden", "metric": "answer_correct", "value": 0.91, "n": 30, "threshold": 0.8,
         "passed": True},
        {"bu": "all", "suite": "leak", "metric": "leaks", "value": 0, "n": 34, "threshold": 0, "passed": True},
    ]  # fmt: skip
    with as_pipe("svc:eval") as c:
        c.execute("SELECT kb.record_eval_run(%s, %s)", (Jsonb(run), Jsonb(results)))
    body = get(client, "/ops/eval?limit=5", OPS).json()
    latest = body["runs"][0]  # started in 2030: newest
    assert latest["run_id"] == run_id and latest["status"] == "PASS" and latest["judge"] == "llama3.1:8b"
    assert {(r["suite"], r["metric"], r["value"]) for r in latest["results"]} == {
        ("golden", "answer_correct", 0.91),
        ("leak", "leaks", 0.0),
    }
    assert body["leak"]["run_id"] == run_id and body["leak"]["leaks"] == 0 and body["leak"]["tests"] == 34


def test_ops_payloads_carry_no_question_text_or_chunk_ids(client, as_api):
    secret_q = f"canary question {uuid.uuid4().hex}"
    qid = str(uuid.uuid4())
    with as_api("ret.user1") as c:
        c.execute(
            "INSERT INTO kb.query_log (query_id, user_id, question) VALUES (%s, kb.current_actor(), %s)",
            (qid, secret_q),
        )
    blob = json.dumps([get(client, p, OPS).json() for p in ENDPOINTS])
    assert secret_q not in blob and "chunk_id" not in blob and "question" not in blob
