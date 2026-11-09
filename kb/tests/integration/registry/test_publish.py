"""Uploads: PUBLISH_DOCUMENT (four-eyes) and UNPUBLISH_DOCUMENT (single actor), CLAUDE.md section 5."""

from __future__ import annotations

import pytest
from psycopg import errors as pg

from helpers import insert_upload, onboard, owner_session


@pytest.fixture()
def space(api):
    return onboard(api, "retail", "UPLOAD")


@pytest.fixture()
def doc(space, as_pipe):
    with as_pipe() as c:
        return insert_upload(c, space["source_id"], "ret.user1")


def visibility(as_admin, doc_id: str) -> str:
    with as_admin() as c:
        return c.execute("SELECT visibility FROM kb.document WHERE doc_id = %s", (doc_id,)).fetchone()["visibility"]


def test_upload_space_is_an_onboarded_upload_source(api, space, as_admin):
    with as_admin() as c:
        s = c.execute("SELECT * FROM kb.kb_source WHERE source_id = %s", (space["source_id"],)).fetchone()
    assert s["source_type"] == "UPLOAD" and s["status"] == "ACTIVE" and s["sp_site_id"] is None


def test_upload_space_cannot_carry_sharepoint_fields(api):
    body = {
        "source_id": "ret-bad-space",
        "bu": "retail",
        "display_name": "x",
        "source_type": "UPLOAD",
        "sp_site_id": "site",
        "revision": {
            "classification": "INTERNAL",
            "retention_class": "R7Y",
            "acl_groups": ["GRP_KB_RETAIL_READ"],
            "allowed_extensions": ["pdf"],
            "max_file_bytes": 10,
        },
    }
    assert api.call("POST", "/sources", "ret.maker", body).status_code == 422


def test_approved_publish_makes_document_published(api, doc, as_admin):
    r = api.call("POST", f"/documents/{doc}/publish", "ret.user1", {"comment": "please share"})
    assert r.status_code == 201, r.text
    req = r.json()["request_id"]
    assert visibility(as_admin, doc) == "PRIVATE"  # nothing changes until a checker approves
    inbox = api.call("GET", "/inbox", "ret.checker").json()
    assert req in {i["request_id"] for i in inbox}
    detail = api.call("GET", f"/requests/{req}", "ret.checker").json()
    assert detail["acl_change"] == {"from": ["user:ret.user1"], "to": ["GRP_KB_RETAIL_READ"]}
    assert api.call("POST", f"/requests/{req}/approve", "ret.checker", {}).status_code == 200
    assert visibility(as_admin, doc) == "PUBLISHED"


def test_rejected_publish_stays_private(api, doc, as_admin):
    req = api.call("POST", f"/documents/{doc}/publish", "ret.user1", {}).json()["request_id"]
    r = api.call("POST", f"/requests/{req}/reject", "ret.checker", {"reason": "contains draft figures"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert visibility(as_admin, doc) == "PRIVATE"


def test_only_uploader_can_request_publish(api, doc):
    for user in ("ret.user2", "ret.maker", "ret.checker"):
        assert api.call("POST", f"/documents/{doc}/publish", user, {}).status_code == 403


def test_uploader_cannot_approve_own_publish_even_as_checker(regdb, api, space, as_pipe, as_api):
    """ret.checker uploads a file and requests publication: they cannot approve it themselves."""
    with as_pipe() as c:
        own = insert_upload(c, space["source_id"], "ret.checker")
    req = api.call("POST", f"/documents/{own}/publish", "ret.checker", {}).json()["request_id"]
    assert req not in {i["request_id"] for i in api.call("GET", "/inbox", "ret.checker").json()}
    with as_api("ret.checker") as c, pytest.raises(pg.InsufficientPrivilege, match="maker cannot check"):
        c.execute("SELECT kb.decide_request(%s, true, 'self')", (req,))


def test_checker_of_other_bu_cannot_approve_publish(api, doc):
    req = api.call("POST", f"/documents/{doc}/publish", "ret.user1", {}).json()["request_id"]
    assert api.call("POST", f"/requests/{req}/approve", "corp.checker", {}).status_code == 403
    assert api.call("GET", f"/requests/{req}", "corp.checker").status_code == 404


def test_one_pending_publish_per_document(api, doc):
    assert api.call("POST", f"/documents/{doc}/publish", "ret.user1", {}).status_code == 201
    assert api.call("POST", f"/documents/{doc}/publish", "ret.user1", {}).status_code == 409


def test_unpublish_is_single_actor_and_restricted(api, doc, as_admin):
    req = api.call("POST", f"/documents/{doc}/publish", "ret.user1", {}).json()["request_id"]
    api.call("POST", f"/requests/{req}/approve", "ret.checker", {})
    assert api.call("POST", f"/documents/{doc}/unpublish", "ret.user2", {"reason": "x"}).status_code == 403
    assert api.call("POST", f"/documents/{doc}/unpublish", "corp.checker", {"reason": "x"}).status_code == 403
    assert api.call("POST", f"/documents/{doc}/unpublish", "ret.user1", {}).status_code == 422  # reason required
    r = api.call("POST", f"/documents/{doc}/unpublish", "ret.maker", {"reason": "superseded by policy"})
    assert r.status_code == 200, r.text
    assert visibility(as_admin, doc) == "PRIVATE"
    with as_admin() as c:
        row = c.execute("SELECT * FROM kb.approval_request WHERE request_id = %s", (r.json()["request_id"],)).fetchone()
    assert (row["request_type"], row["status"], row["maker_id"], row["checker_id"]) == (
        "UNPUBLISH_DOCUMENT",
        "EXECUTED",
        "ret.maker",
        None,
    )


def test_executed_status_only_for_unpublish(regdb, doc, space):
    with owner_session(regdb) as c:
        with pytest.raises(pg.CheckViolation):
            c.execute(
                "INSERT INTO kb.approval_request (request_type, source_id, doc_id, maker_id, status, decided_at) "
                "VALUES ('PUBLISH_DOCUMENT', %s, %s, 'x', 'EXECUTED', now())",
                (space["source_id"], doc),
            )
        with pytest.raises(pg.CheckViolation):
            c.execute(
                "INSERT INTO kb.approval_request (request_type, source_id, doc_id, maker_id) "
                "VALUES ('UNPUBLISH_DOCUMENT', %s, %s, 'x')",
                (space["source_id"], doc),
            )


def test_pipeline_cannot_insert_published_or_flip_visibility(space, doc, as_pipe):
    with as_pipe() as c:
        with pytest.raises(pg.InsufficientPrivilege, match="uploads start PRIVATE"):
            c.execute(
                "INSERT INTO kb.document (doc_id, source_id, external_item_id, current_path, origin, uploaded_by, "
                "visibility) VALUES ('d-pub', %s, 'd-pub', 'x', 'UPLOAD', 'ret.user1', 'PUBLISHED')",
                (space["source_id"],),
            )
        with pytest.raises(pg.InsufficientPrivilege, match="visibility changes only"):
            c.execute("UPDATE kb.document SET visibility = 'PUBLISHED' WHERE doc_id = %s", (doc,))
        with pytest.raises(pg.InsufficientPrivilege, match="immutable"):
            c.execute("UPDATE kb.document SET uploaded_by = 'ret.user2' WHERE doc_id = %s", (doc,))


def test_upload_origin_must_match_source_type(api, as_pipe):
    sp = onboard(api, "retail", "SHAREPOINT")
    with as_pipe() as c, pytest.raises(pg.InsufficientPrivilege, match="does not match source type"):
        insert_upload(c, sp["source_id"], "ret.user1")


def test_delete_upload_by_uploader_withdraws_pending_publish(api, doc, as_api, as_admin):
    req = api.call("POST", f"/documents/{doc}/publish", "ret.user1", {}).json()["request_id"]
    with as_api("ret.user2") as c, pytest.raises(pg.InsufficientPrivilege, match="only the uploader"):
        c.execute("SELECT kb.delete_upload(%s)", (doc,))
    with as_api("ret.user1") as c:
        c.execute("SELECT kb.delete_upload(%s)", (doc,))
    with as_admin() as c:
        d = c.execute("SELECT status, deleted_at FROM kb.document WHERE doc_id = %s", (doc,)).fetchone()
        r = c.execute("SELECT status FROM kb.approval_request WHERE request_id = %s", (req,)).fetchone()
    assert d["status"] == "DELETED" and d["deleted_at"] is not None and r["status"] == "WITHDRAWN"
    assert api.call("POST", f"/requests/{req}/approve", "ret.checker", {}).status_code == 403


def test_publish_into_suspended_space_refused(regdb, api, doc, space, as_api):
    with as_api("ret.checker") as c:
        c.execute("SELECT kb.suspend_source(%s, 'review')", (space["source_id"],))
    assert api.call("POST", f"/documents/{doc}/publish", "ret.user1", {}).status_code == 409
