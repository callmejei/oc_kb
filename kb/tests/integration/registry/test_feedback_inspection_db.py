"""Migration 007 in the database: feedback (F1-F3), inspector labels (I1), re-parse queue (R1),
exclude / include (X1, X2). Every rule is exercised as the real API or pipeline role."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from psycopg.types.json import Jsonb

from helpers import insert_upload, onboard, uid


def sp_doc(pipe, source_id: str, indexed: bool = True) -> tuple[str, str]:
    doc = uid("doc")
    pipe.execute(
        """INSERT INTO kb.document (doc_id, source_id, external_item_id, current_path, title, origin)
           VALUES (%s, %s, %s, %s, 'Policy', 'SHAREPOINT')""",
        (doc, source_id, doc, f"/KB/{doc}.pdf"),
    )
    pipe.execute(
        """INSERT INTO kb.document_version (version_id, doc_id, version_no, sha256, size_bytes, raw_uri, status,
                                            is_current)
           VALUES (%s, %s, 1, %s, 10, %s, %s, %s)""",
        (f"{doc}:v1", doc, "b" * 64, f"file:///raw/{doc}", "INDEXED" if indexed else "RECEIVED", indexed),
    )
    return doc, f"{doc}:v1"


@pytest.fixture()
def world(api, as_pipe):
    """Two retail docs, one wealth doc, one private retail upload."""
    retail = onboard(api, "retail")["source_id"]
    wealth = onboard(api, "wealth")["source_id"]
    space = onboard(api, "retail", "UPLOAD")["source_id"]
    with as_pipe() as c:
        r1, r1v = sp_doc(c, retail)
        _r2, _ = sp_doc(c, retail)
        w1, w1v = sp_doc(c, wealth)
        up = insert_upload(c, space, "ret.user1")
    return {"retail": retail, "wealth": wealth, "r1": r1, "r1v": r1v, "w1": w1, "w1v": w1v, "up": up}


def query(as_api, user: str, cited: list[str]) -> str:
    """What AskService writes: one QUERY audit row per question (actor forced to the session's user)."""
    qid = str(uuid.uuid4())
    with as_api(user) as c:
        c.execute(
            "INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, details) "
            "VALUES (kb.current_actor(), 'QUERY', 'query', %s, %s)",
            (qid, Jsonb({"cited_doc_versions": cited})),
        )
    return qid


# ------------------------------------------------------------------------------------------- feedback
def test_feedback_targets_come_from_the_askers_own_audit_row(world, as_api):
    w = world
    qid = query(as_api, "data.steward", [w["r1v"], w["w1v"], f"{w['up']}:v1"])  # cites 2 BUs + a private upload
    with as_api("data.steward") as c:
        fid = c.execute(
            "SELECT kb.submit_feedback(%s, 'DOWN', 'TABLE_BROKEN', 'LTV row wrong') AS f", (qid,)
        ).fetchone()["f"]
        rows = c.execute("SELECT bu, version_ids FROM kb.feedback_target WHERE feedback_id = %s ORDER BY bu", (fid,))
        targets = {r["bu"]: r["version_ids"] for r in rows}
    assert targets == {"retail": [w["r1v"]], "wealth": [w["w1v"]]}  # private upload never becomes a target (F2)


def test_only_the_asker_can_flag_and_only_once(world, as_api):
    qid = query(as_api, "ret.user1", [world["r1v"]])
    with as_api("ret.user2") as c, pytest.raises(pg.NoDataFound, match="unknown query"):
        c.execute("SELECT kb.submit_feedback(%s, 'DOWN', 'OTHER', 'x')", (qid,))
    with as_api("ret.user1") as c:
        c.execute("SELECT kb.submit_feedback(%s, 'DOWN', 'OTHER', 'x')", (qid,))
        with pytest.raises(pg.UniqueViolation):
            c.execute("SELECT kb.submit_feedback(%s, 'UP', NULL, NULL)", (qid,))


def test_thumbs_down_needs_a_problem_and_reason(world, as_api):
    qid = query(as_api, "ret.user1", [world["r1v"]])
    with as_api("ret.user1") as c, pytest.raises(pg.CheckViolation):
        c.execute("SELECT kb.submit_feedback(%s, 'DOWN', 'OTHER', '')", (qid,))


def test_feedback_is_immutable_and_resolved_only_by_the_bus_governance(world, as_api, as_admin):
    qid = query(as_api, "ret.user1", [world["r1v"]])
    with as_api("ret.user1") as c:
        fid = c.execute("SELECT kb.submit_feedback(%s, 'DOWN', 'OTHER', 'wrong') AS f", (qid,)).fetchone()["f"]
    with as_admin() as c, pytest.raises(pg.InsufficientPrivilege, match="answer_feedback is append-only"):
        c.execute("UPDATE kb.answer_feedback SET reason = 'edited' WHERE feedback_id = %s", (fid,))
    with as_api("wlth.maker") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("SELECT kb.resolve_feedback(%s, 'retail', 'fixed')", (fid,))
    with as_api("ret.maker") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("UPDATE kb.feedback_target SET status = 'RESOLVED' WHERE feedback_id = %s", (fid,))
    with as_api("ret.checker") as c:
        c.execute("SELECT kb.resolve_feedback(%s, 'retail', 're-parsed with full-page OCR')", (fid,))
        with pytest.raises(pg.InsufficientPrivilege, match="already RESOLVED"):
            c.execute("SELECT kb.resolve_feedback(%s, 'retail', 'again')", (fid,))
    with as_admin() as c:
        a = c.execute(
            "SELECT actor_id FROM kb.audit_log WHERE object_type = 'feedback_target' AND object_id = %s", (str(fid),)
        ).fetchone()
    assert a["actor_id"] == "ret.checker"


# ------------------------------------------------------------------------------------------- labels
def test_inspection_labels_by_the_bus_governance_only(world, as_api):
    w = world
    with as_api("ret.maker") as c:
        c.execute("SELECT kb.add_inspection_label(%s, 2, '#/tables/0', 'TABLE_BROKEN', 'merged cells')", (w["r1v"],))
    with as_api("wlth.maker") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("SELECT kb.add_inspection_label(%s, 1, NULL, 'OCR_ERROR', NULL)", (w["r1v"],))
    with as_api("ret.maker") as c, pytest.raises(pg.NoDataFound):  # a private upload is not governance's to label
        c.execute("SELECT kb.add_inspection_label(%s, 1, NULL, 'OCR_ERROR', NULL)", (f"{w['up']}:v1",))


# ------------------------------------------------------------------------------------------- re-parse
def test_reparse_is_queued_by_a_maker_with_allow_listed_options(world, as_api, as_pipe):
    w = world
    for user in ("ret.checker", "ret.user1", "wlth.maker"):
        with as_api(user) as c, pytest.raises(pg.InsufficientPrivilege):
            c.execute("SELECT kb.request_reparse(%s, '{}', 'x')", (w["r1"],))
    with as_api("ret.maker") as c:
        for bad in ({"shell": "rm"}, {"ocr_mode": "remote"}, {"table_mode": "slow"}):
            with pytest.raises(pg.RaiseException):
                c.execute("SELECT kb.request_reparse(%s, %s, 'x')", (w["r1"], Jsonb(bad)))
        rid = c.execute(
            "SELECT kb.request_reparse(%s, %s, 'OCR garbled page 2') AS r", (w["r1"], Jsonb({"ocr_mode": "full_page"}))
        ).fetchone()["r"]
        with pytest.raises(pg.UniqueViolation):  # one queued per document
            c.execute("SELECT kb.request_reparse(%s, '{}', 'again')", (w["r1"],))
        with pytest.raises(pg.NoDataFound):  # never on a private upload
            c.execute("SELECT kb.request_reparse(%s, '{}', 'x')", (w["up"],))
    with as_pipe() as c:
        with pytest.raises(pg.InsufficientPrivilege):
            c.execute("SELECT kb.request_reparse(%s, '{}', 'x')", (w["r1"],))
        with pytest.raises(pg.InsufficientPrivilege):  # the pipeline records outcomes only
            c.execute("UPDATE kb.reparse_request SET options = '{}' WHERE reparse_id = %s", (rid,))
        c.execute(
            "UPDATE kb.reparse_request SET status = 'FAILED', error = 'x', finished_at = now() WHERE reparse_id = %s",
            (rid,),
        )
        with pytest.raises(pg.InsufficientPrivilege, match="already FAILED"):
            c.execute(
                "UPDATE kb.reparse_request SET status = 'QUEUED', finished_at = NULL WHERE reparse_id = %s", (rid,)
            )


# ------------------------------------------------------------------------------------------- exclude / include
def doc_row(as_admin, doc: str) -> dict:
    with as_admin() as c:
        return c.execute("SELECT status, restore_pending FROM kb.document WHERE doc_id = %s", (doc,)).fetchone()


def test_exclude_is_single_actor_and_include_is_four_eyes(world, as_api, as_pipe, as_admin, api):
    w = world
    with as_api("ret.user1") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("SELECT kb.exclude_document(%s, 'x')", (w["r1"],))
    with as_api("ret.checker") as c:
        req = c.execute("SELECT kb.exclude_document(%s, 'contains a draft') AS r", (w["r1"],)).fetchone()["r"]
        assert (
            c.execute("SELECT status FROM kb.approval_request WHERE request_id = %s", (req,)).fetchone()["status"]
            == "EXECUTED"
        )
    assert doc_row(as_admin, w["r1"])["status"] == "EXCLUDED"
    # nobody but the governance functions can bring it back: not the pipeline, not the API role directly
    with as_pipe() as c, pytest.raises(pg.InsufficientPrivilege, match="EXCLUDED"):
        c.execute("UPDATE kb.document SET status = 'ACTIVE' WHERE doc_id = %s", (w["r1"],))
    with as_pipe() as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("UPDATE kb.document SET restore_pending = true WHERE doc_id = %s", (w["r1"],))
    with as_api("ret.checker") as c, pytest.raises(pg.InsufficientPrivilege):  # include: MAKER requests
        c.execute("SELECT kb.request_include(%s, 'x')", (w["r1"],))
    with as_api("ret.maker") as c:
        inc = c.execute("SELECT kb.request_include(%s, 'draft removed upstream') AS r", (w["r1"],)).fetchone()["r"]
    assert api.call("POST", f"/requests/{inc}/approve", "ret.maker", {}).status_code == 403  # maker != checker
    assert api.call("POST", f"/requests/{inc}/approve", "wlth.checker", {}).status_code == 403
    assert doc_row(as_admin, w["r1"])["status"] == "EXCLUDED"
    assert api.call("POST", f"/requests/{inc}/approve", "ret.checker", {"reason": "ok"}).status_code == 200
    assert doc_row(as_admin, w["r1"]) == {"status": "ACTIVE", "restore_pending": True}
    with as_pipe() as c:  # the pipeline may clear the flag once it has restored the index
        c.execute("UPDATE kb.document SET restore_pending = false WHERE doc_id = %s", (w["r1"],))


def test_exclude_cancels_a_queued_reparse_and_blocks_new_ones(world, as_api, as_admin):
    w = world
    with as_api("ret.maker") as c:
        rid = c.execute("SELECT kb.request_reparse(%s, '{}', 'x') AS r", (w["r1"],)).fetchone()["r"]
        c.execute("SELECT kb.exclude_document(%s, 'withdrawn')", (w["r1"],))
        with pytest.raises(pg.RaiseException, match="EXCLUDED"):
            c.execute("SELECT kb.request_reparse(%s, '{}', 'x')", (w["r1"],))
    with as_admin() as c:
        assert (
            c.execute("SELECT status FROM kb.reparse_request WHERE reparse_id = %s", (rid,)).fetchone()["status"]
            == "FAILED"
        )


def test_private_uploads_cannot_be_excluded_by_governance(world, as_api):
    with as_api("ret.maker") as c, pytest.raises(pg.NoDataFound):
        c.execute("SELECT kb.exclude_document(%s, 'x')", (world["up"],))
