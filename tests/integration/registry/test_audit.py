"""Phase 1 acceptance: every governance change writes an audit row (actor = the API caller)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from psycopg import errors as pg

from helpers import CHECKER, MAKER, insert_upload, onboard, register, revision_body


def audit_since(as_admin: Callable[..., Any], mark: int) -> list[dict[str, Any]]:
    with as_admin() as c:
        return c.execute(
            "SELECT actor_id, action, object_type, object_id, before_state, after_state, details "
            "FROM kb.audit_log WHERE audit_id > %s ORDER BY audit_id",
            (mark,),
        ).fetchall()


@pytest.fixture()
def mark(as_admin) -> Callable[[], int]:
    def _m() -> int:
        with as_admin() as c:
            return int(c.execute("SELECT coalesce(max(audit_id), 0) AS m FROM kb.audit_log").fetchone()["m"])

    return _m


def kinds(rows: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {(r["actor_id"], r["action"], r["object_type"]) for r in rows}


def test_register_source_audited(api, as_admin, mark):
    m = mark()
    created = register(api, "retail")
    rows = audit_since(as_admin, m)
    assert kinds(rows) == {("ret.maker", "INSERT", "kb_source"), ("ret.maker", "INSERT", "source_revision")}
    assert {r["object_id"] for r in rows} == {created["source"]["source_id"], created["revision"]["revision_id"]}


def test_edit_draft_audited_with_before_and_after(api, as_admin, mark):
    created = register(api, "retail")
    rid = created["revision"]["revision_id"]
    m = mark()
    r = api.call("PATCH", f"/revisions/{rid}", "ret.maker", {"retention_class": "R10Y"})
    assert r.status_code == 200, r.text
    (row,) = audit_since(as_admin, m)
    assert (row["actor_id"], row["action"], row["object_type"]) == ("ret.maker", "UPDATE", "source_revision")
    assert row["before_state"]["retention_class"] == "R7Y" and row["after_state"]["retention_class"] == "R10Y"


def test_submit_withdraw_approve_reject_audited(api, as_admin, mark):
    a = register(api, "retail")
    m = mark()
    req = api.call("POST", f"/revisions/{a['revision']['revision_id']}/submit", "ret.maker", {}).json()["request_id"]
    assert kinds(audit_since(as_admin, m)) == {
        ("ret.maker", "UPDATE", "source_revision"),
        ("ret.maker", "INSERT", "approval_request"),
    }
    m = mark()
    assert api.call("POST", f"/requests/{req}/withdraw", "ret.maker", {"reason": "redo"}).status_code == 200
    assert kinds(audit_since(as_admin, m)) == {
        ("ret.maker", "UPDATE", "approval_request"),
        ("ret.maker", "UPDATE", "source_revision"),
    }

    b = register(api, "retail")
    req = api.call("POST", f"/revisions/{b['revision']['revision_id']}/submit", "ret.maker", {}).json()["request_id"]
    m = mark()
    assert api.call("POST", f"/requests/{req}/reject", "ret.checker", {"reason": "acl too broad"}).status_code == 200
    rows = audit_since(as_admin, m)
    assert kinds(rows) == {("ret.checker", "UPDATE", "approval_request"), ("ret.checker", "UPDATE", "source_revision")}
    assert next(r for r in rows if r["object_type"] == "approval_request")["after_state"]["status"] == "REJECTED"

    c = register(api, "retail")
    req = api.call("POST", f"/revisions/{c['revision']['revision_id']}/submit", "ret.maker", {}).json()["request_id"]
    m = mark()
    assert api.call("POST", f"/requests/{req}/approve", "ret.checker", {}).status_code == 200
    assert kinds(audit_since(as_admin, m)) == {
        ("ret.checker", "UPDATE", "approval_request"),
        ("ret.checker", "UPDATE", "source_revision"),
        ("ret.checker", "UPDATE", "kb_source"),
    }


def test_change_source_supersede_audited(api, as_admin, mark):
    src = onboard(api, "corporate")
    rev = api.call("POST", f"/sources/{src['source_id']}/revisions", MAKER["corporate"], {"retention_class": "R10Y"})
    req = api.call("POST", f"/revisions/{rev.json()['revision_id']}/submit", MAKER["corporate"], {}).json()
    m = mark()
    api.call("POST", f"/requests/{req['request_id']}/approve", CHECKER["corporate"], {})
    rows = [r for r in audit_since(as_admin, m) if r["object_type"] == "source_revision"]
    assert sorted(r["after_state"]["status"] for r in rows) == ["APPROVED", "SUPERSEDED"]


def test_publish_unpublish_and_upload_receipt_audited(api, as_admin, as_pipe, mark):
    space = onboard(api, "retail", "UPLOAD")
    m = mark()
    with as_pipe() as c:
        doc = insert_upload(c, space["source_id"], "ret.user1", ip="10.20.30.40")
    (receipt,) = audit_since(as_admin, m)
    assert receipt["action"] == "UPLOAD_RECEIVED"
    assert receipt["details"]["uploaded_by"] == "ret.user1"
    assert receipt["details"]["source_ip"] == "10.20.30.40"
    assert receipt["details"]["sha256"] == "a" * 64 and receipt["details"]["received_at"]

    m = mark()
    req = api.call("POST", f"/documents/{doc}/publish", "ret.user1", {"comment": "useful for branch"}).json()
    api.call("POST", f"/requests/{req['request_id']}/approve", "ret.checker", {})
    rows = audit_since(as_admin, m)
    assert ("ret.user1", "INSERT", "approval_request") in kinds(rows)
    vis = [r for r in rows if r["object_type"] == "document"]
    assert len(vis) == 1 and vis[0]["actor_id"] == "ret.checker"
    assert (vis[0]["before_state"]["visibility"], vis[0]["after_state"]["visibility"]) == ("PRIVATE", "PUBLISHED")

    m = mark()
    api.call("POST", f"/documents/{doc}/unpublish", "ret.user1", {"reason": "outdated"})
    rows = audit_since(as_admin, m)
    assert kinds(rows) == {("ret.user1", "UPDATE", "document"), ("ret.user1", "INSERT", "approval_request")}
    assert next(r for r in rows if r["object_type"] == "approval_request")["after_state"]["status"] == "EXECUTED"


def test_rolesync_audited_and_idempotent(regdb, as_admin, mark):
    from kb.adapters.local.identity import YamlIdentityProvider
    from kb.registry import rolesync

    m = mark()
    assert rolesync.sync(regdb, YamlIdentityProvider("config/users.yaml")) == (0, 0)
    assert audit_since(as_admin, m) == []
    with as_admin() as c:
        rows = c.execute(
            "SELECT DISTINCT actor_id FROM kb.audit_log WHERE object_type = 'governance_role' "
            "AND object_id = 'ret.checker'"
        ).fetchall()
    assert {r["actor_id"] for r in rows} == {"svc:rolesync"}


def test_audit_log_is_append_only_for_everyone(as_api, as_pipe, as_admin):
    with as_api("ret.maker") as c:
        with pytest.raises(pg.InsufficientPrivilege):
            c.execute("UPDATE kb.audit_log SET actor_id = 'x'")
        with pytest.raises(pg.InsufficientPrivilege):
            c.execute("DELETE FROM kb.audit_log")
    with as_pipe() as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("DELETE FROM kb.audit_log")
    with as_admin() as c:
        for stmt in ("UPDATE kb.audit_log SET actor_id = 'x'", "DELETE FROM kb.audit_log", "TRUNCATE kb.audit_log"):
            with pytest.raises(pg.InsufficientPrivilege, match="append-only"):
                c.execute(stmt)


def test_every_audit_row_has_an_actor(as_admin):
    with as_admin() as c:
        n = c.execute("SELECT count(*) AS n FROM kb.audit_log WHERE coalesce(actor_id, '') = ''").fetchone()["n"]
    assert n == 0


def test_body_helper_matches_schema():
    assert set(revision_body()) <= {
        "classification",
        "retention_class",
        "acl_groups",
        "allowed_extensions",
        "max_file_bytes",
        "include_paths",
        "exclude_paths",
        "pii_policy",
    }
