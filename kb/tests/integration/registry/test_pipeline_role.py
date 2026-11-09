"""Phase 1 acceptance: the pipeline role can't approve (two independent layers)."""

from __future__ import annotations

import pytest
from psycopg import errors as pg

from helpers import onboard, register


@pytest.fixture()
def pending(api, as_api):
    created = register(api, "retail")
    with as_api("ret.maker") as c:
        req = c.execute(
            "SELECT kb.submit_request('ONBOARD_SOURCE', %s, %s) AS id",
            (created["source"]["source_id"], created["revision"]["revision_id"]),
        ).fetchone()["id"]
    return req


@pytest.mark.parametrize(
    "stmt",
    [
        "SELECT kb.decide_request(%s, true, 'pipeline approves')",
        "SELECT kb.withdraw_request(%s, 'x')",
    ],
)
def test_pipeline_has_no_execute_on_decision_functions(as_pipe, pending, stmt):
    with as_pipe("ret.checker") as c, pytest.raises(pg.InsufficientPrivilege, match="permission denied for function"):
        c.execute(stmt, (pending,))


def test_pipeline_cannot_update_requests_directly(as_pipe, pending):
    with as_pipe("ret.checker") as c, pytest.raises(pg.InsufficientPrivilege, match="permission denied"):
        c.execute(
            "UPDATE kb.approval_request SET status='APPROVED', checker_id='ret.checker', decided_at=now() "
            "WHERE request_id=%s",
            (pending,),
        )


def test_pipeline_cannot_submit_or_publish(as_pipe, api):
    src = onboard(api, "retail")
    with as_pipe() as c:
        with pytest.raises(pg.InsufficientPrivilege):
            c.execute("SELECT kb.submit_request('REACTIVATE_SOURCE', %s)", (src["source_id"],))
        with pytest.raises(pg.InsufficientPrivilege):
            c.execute("SELECT kb.request_publish('x', 'y')")


def test_second_layer_refuses_pipeline_even_if_execute_were_granted(regdb, as_admin, as_pipe, pending):
    """Defence in depth, proven in the throwaway kb_test database only: grant EXECUTE to the pipeline
    role, show the session_user guard still refuses, then revoke. Production grants are untouched."""
    with as_admin() as c:
        c.execute("GRANT EXECUTE ON FUNCTION kb.decide_request(uuid, boolean, text) TO kb_pipeline")
    try:
        with as_pipe("ret.checker") as c, pytest.raises(pg.InsufficientPrivilege, match="pipeline role cannot decide"):
            c.execute("SELECT kb.decide_request(%s, true, 'x')", (pending,))
    finally:
        with as_admin() as c:
            c.execute("REVOKE EXECUTE ON FUNCTION kb.decide_request(uuid, boolean, text) FROM kb_pipeline")
    with as_admin() as c:
        granted = c.execute(
            "SELECT has_function_privilege('kb_pipeline', 'kb.decide_request(uuid,boolean,text)', 'EXECUTE') AS g"
        ).fetchone()
    assert granted["g"] is False


def test_pipeline_cannot_touch_governance_tables(as_pipe, api):
    src = onboard(api, "retail")
    with as_pipe() as c:
        for stmt in (
            "UPDATE kb.kb_source SET status='SUSPENDED' WHERE source_id = %s",
            "UPDATE kb.source_revision SET acl_groups='{GRP_KB_CORPORATE_READ}' WHERE source_id = %s",
            "INSERT INTO kb.governance_role VALUES (%s, 'retail', 'CHECKER', now())",
        ):
            with pytest.raises(pg.InsufficientPrivilege):
                c.execute(stmt, (src["source_id"],))
