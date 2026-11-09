"""Phase 1 acceptance: maker != checker, locked revisions, pipeline cannot approve - in the DATABASE.

Every attack here uses direct SQL, bypassing the API, to prove the database itself refuses.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors as pg

from helpers import onboard, owner_session, register, uid

SUBMIT = "SELECT kb.submit_request('ONBOARD_SOURCE', %s, %s) AS id"


@pytest.fixture()
def pending(api, as_api):
    """A freshly submitted retail ONBOARD request (maker ret.maker)."""
    created = register(api, "retail")
    sid, rid = created["source"]["source_id"], created["revision"]["revision_id"]
    with as_api("ret.maker") as c:
        req = c.execute(SUBMIT, (sid, rid)).fetchone()["id"]
    return {"source_id": sid, "revision_id": rid, "request_id": req}


@pytest.fixture()
def dual_role_user(regdb):
    """A user who is both MAKER and CHECKER of retail (not in users.yaml; added for this attack)."""
    user = uid("dual")
    with owner_session(regdb) as c:
        c.execute(
            "INSERT INTO kb.governance_role (actor_id, bu, role) VALUES (%s,'retail','MAKER'), (%s,'retail','CHECKER')",
            (user, user),
        )
    yield user
    with owner_session(regdb) as c:
        c.execute("DELETE FROM kb.governance_role WHERE actor_id = %s", (user,))


# ---------------------------------------------------------------------------- maker != checker
def test_maker_cannot_approve_own_request_even_holding_checker_role(api, as_api, dual_role_user):
    created = register(api, "retail")
    with as_api(dual_role_user) as c:
        # dual user re-authors a draft revision of their own and submits it
        rev = c.execute(
            "INSERT INTO kb.source_revision (source_id, revision_no, classification, retention_class, acl_groups, "
            "allowed_extensions, max_file_bytes, created_by) VALUES (%s, 2, 'INTERNAL', 'R7Y', "
            "'{GRP_KB_RETAIL_READ}', '{pdf}', 100, 'someone-else') RETURNING revision_id, created_by",
            (created["source"]["source_id"],),
        ).fetchone()
        assert rev["created_by"] == dual_role_user  # authorship cannot be forged (003)
        req = c.execute(SUBMIT, (created["source"]["source_id"], rev["revision_id"])).fetchone()["id"]
        with pytest.raises(pg.InsufficientPrivilege, match="maker cannot check their own request"):
            c.execute("SELECT kb.decide_request(%s, true, 'self-approve')", (req,))


def test_four_eyes_check_constraint_holds_even_inside_owner(regdb, pending):
    with owner_session(regdb, "ret.maker") as c, pytest.raises(pg.CheckViolation, match="four_eyes"):
        c.execute(
            "UPDATE kb.approval_request SET status='APPROVED', checker_id = maker_id, decided_at = now() "
            "WHERE request_id = %s",
            (pending["request_id"],),
        )


def test_checker_of_another_bu_cannot_approve(as_api, pending):
    with as_api("corp.checker") as c, pytest.raises(pg.InsufficientPrivilege, match="not a CHECKER for BU retail"):
        c.execute("SELECT kb.decide_request(%s, true, 'x')", (pending["request_id"],))


def test_ordinary_user_cannot_approve(as_api, pending):
    with as_api("ret.user1") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("SELECT kb.decide_request(%s, true, 'x')", (pending["request_id"],))


def test_app_role_cannot_update_or_insert_requests_directly(as_api, pending):
    with as_api("ret.checker") as c:
        with pytest.raises(pg.InsufficientPrivilege, match="permission denied"):
            c.execute(
                "UPDATE kb.approval_request SET status='APPROVED', checker_id='ret.checker', decided_at=now() "
                "WHERE request_id = %s",
                (pending["request_id"],),
            )
        with pytest.raises(pg.InsufficientPrivilege, match="permission denied"):
            c.execute(
                "INSERT INTO kb.approval_request (request_type, source_id, maker_id, status, checker_id, "
                "decided_at) VALUES ('REACTIVATE_SOURCE', %s, 'x', 'APPROVED', 'y', now())",
                (pending["source_id"],),
            )


def test_even_superuser_cannot_decide_outside_governance_functions(as_admin, pending):
    with as_admin("ret.checker") as c, pytest.raises(pg.InsufficientPrivilege, match="governance functions"):
        c.execute(
            "UPDATE kb.approval_request SET status='APPROVED', checker_id='ret.checker', decided_at=now() "
            "WHERE request_id = %s",
            (pending["request_id"],),
        )


def test_decisions_are_final(as_api, pending):
    with as_api("ret.checker") as c:
        c.execute("SELECT kb.decide_request(%s, true, 'ok')", (pending["request_id"],))
        with pytest.raises(pg.InsufficientPrivilege, match="decisions are final"):
            c.execute("SELECT kb.decide_request(%s, false, 'changed my mind')", (pending["request_id"],))


def test_reject_requires_reason(as_api, pending):
    with as_api("ret.checker") as c, pytest.raises(pg.CheckViolation, match="reject_reason"):
        c.execute("SELECT kb.decide_request(%s, false, NULL)", (pending["request_id"],))


def test_no_actor_no_decision(as_api, pending):
    with as_api(None) as c, pytest.raises(psycopg.Error, match="kb.actor_id is not set"):
        c.execute("SELECT kb.decide_request(%s, true, 'x')", (pending["request_id"],))


def test_only_maker_can_withdraw(as_api, pending):
    with as_api("ret.user1") as c, pytest.raises(pg.InsufficientPrivilege, match="only the maker"):
        c.execute("SELECT kb.withdraw_request(%s, 'x')", (pending["request_id"],))
    with as_api("ret.maker") as c:
        c.execute("SELECT kb.withdraw_request(%s, 'changed plan')", (pending["request_id"],))
        st = c.execute("SELECT status FROM kb.source_revision WHERE revision_id = %s", (pending["revision_id"],))
        assert st.fetchone()["status"] == "WITHDRAWN"


def test_source_cannot_be_activated_without_approval(as_admin, pending):
    with as_admin("ret.maker") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute(
            "UPDATE kb.kb_source SET status='ACTIVE', current_revision_id=%s WHERE source_id=%s",
            (pending["revision_id"], pending["source_id"]),
        )


# ---------------------------------------------------------------------------- locked revisions
CONTENT_EDITS = [
    ("acl_groups", "'{GRP_KB_RETAIL_READ,GRP_KB_CORPORATE_READ}'"),
    ("classification", "'PUBLIC'"),
    ("max_file_bytes", "999999999"),
    ("created_by", "'someone'"),
]


@pytest.mark.parametrize(("col", "val"), CONTENT_EDITS)
def test_locked_revision_content_cannot_change_as_app(as_api, pending, col, val):
    with as_api("ret.maker") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute(f"UPDATE kb.source_revision SET {col} = {val} WHERE revision_id = %s", (pending["revision_id"],))


@pytest.mark.parametrize(("col", "val"), CONTENT_EDITS)
def test_locked_revision_content_cannot_change_even_as_superuser(as_admin, pending, col, val):
    with as_admin("ret.maker") as c, pytest.raises(pg.InsufficientPrivilege, match="locked"):
        c.execute(f"UPDATE kb.source_revision SET {col} = {val} WHERE revision_id = %s", (pending["revision_id"],))


def test_locked_revision_cannot_be_deleted(as_api, as_admin, pending):
    with as_api("ret.maker") as c, pytest.raises(pg.InsufficientPrivilege, match="locked"):
        c.execute("DELETE FROM kb.source_revision WHERE revision_id = %s", (pending["revision_id"],))
    with as_admin("ret.maker") as c, pytest.raises(pg.InsufficientPrivilege, match="locked"):
        c.execute("DELETE FROM kb.source_revision WHERE revision_id = %s", (pending["revision_id"],))


def test_revision_status_cannot_be_set_directly_even_as_superuser(as_admin, pending):
    with as_admin("ret.checker") as c, pytest.raises(pg.InsufficientPrivilege, match="governance functions"):
        c.execute("UPDATE kb.source_revision SET status='APPROVED' WHERE revision_id = %s", (pending["revision_id"],))


def test_approved_revision_stays_locked(api, as_api):
    src = onboard(api, "retail")
    with as_api("ret.maker") as c, pytest.raises(pg.InsufficientPrivilege, match="locked"):
        c.execute("UPDATE kb.source_revision SET retention_class='X' WHERE revision_id = %s", (src["revision_id"],))


def test_draft_is_editable_only_by_maker_of_its_bu(api, as_api):
    created = register(api, "retail")
    rid = created["revision"]["revision_id"]
    with as_api("ret.maker") as c:
        c.execute("UPDATE kb.source_revision SET retention_class='R10Y' WHERE revision_id = %s", (rid,))
    with as_api("corp.maker") as c, pytest.raises(pg.InsufficientPrivilege, match="not a MAKER for BU retail"):
        c.execute("UPDATE kb.source_revision SET retention_class='R1Y' WHERE revision_id = %s", (rid,))
    with as_api("ret.user1") as c, pytest.raises(pg.InsufficientPrivilege, match="not a MAKER"):
        c.execute("DELETE FROM kb.source_revision WHERE revision_id = %s", (rid,))
