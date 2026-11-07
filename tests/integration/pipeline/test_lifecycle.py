"""Phase 2 acceptance: the scripted SharePoint lifecycle, end to end.

onboard -> dry run -> approve -> first load -> edit (new version current, old hidden) -> delete (out of
the index, raw copy kept) -> legal hold blocks purge -> exception released by a checker and reprocessed,
plus: other exception kinds, move out of scope, expired-token resync, idempotent reruns, run limit.
"""

from __future__ import annotations

import shutil
import uuid
from typing import Any

import pytest
from psycopg import errors as pg

from kb.adapters.local.identity import YamlIdentityProvider
from kb.config import load_settings
from kb.ingest.engine import IngestEngine, RunConflictError, doc_id_for
from kb.ingest.factory import build_engine
from kb.ports.types import AccessFilter
from synthetic import scenarios
from synthetic.sharepoint import SharePointSim

from helpers import Api

IDP = YamlIdentityProvider("config/users.yaml")


def flt(user: str) -> AccessFilter:
    return AccessFilter(frozenset(IDP.resolve(user).acl_principals))


@pytest.fixture(scope="module")
def world(tmp_path_factory, data_dir, pipe_dsn, regdb):
    root = tmp_path_factory.mktemp("lifecycle")
    sp = root / "sharepoint"
    shutil.copytree(data_dir / "sharepoint", sp)
    alias = f"kb-test-{uuid.uuid4().hex[:8]}"
    engine = build_engine(
        load_settings("local", strict=True),
        registry_dsn=pipe_dsn,
        index_alias=alias,
        raw_url=(root / "raw").as_uri(),
        processed_url=(root / "processed").as_uri(),
        sharepoint_root=str(sp),
    )
    engine.index.ensure_index()
    yield {"sp": sp, "engine": engine}
    engine.index.drop()
    engine.reg.close()


def item_doc(source_id: str, key: str) -> str:
    return doc_id_for(source_id, SharePointSim.item_id_for(key))


def versions(as_admin, doc_id: str) -> list[dict[str, Any]]:
    with as_admin() as c:
        return c.execute(
            "SELECT version_id, version_no, status, is_current, raw_uri, retention_until FROM kb.document_version "
            "WHERE doc_id = %s ORDER BY version_no",
            (doc_id,),
        ).fetchall()


def visible_versions(engine: IngestEngine, doc_id: str, user: str = "ret.user1") -> set[str]:
    """Versions of doc_id a user can retrieve: the user's real pre-filter plus a doc_id term."""
    from kb.adapters.local.es_index import render_filter

    q = {"bool": {"filter": [*render_filter(flt(user)), {"term": {"doc_id": doc_id}}]}}
    return {d["version_id"] for d in engine.index.docs(q)}


def test_sharepoint_lifecycle_end_to_end(world, api: Api, as_admin, as_api):
    engine: IngestEngine = world["engine"]
    sp = world["sp"]
    raw_store = engine.raw

    # ---- 1. onboard: maker registers + submits; dry run; checker sees report and approves -------------
    body = {
        "source_id": f"ret-kb-{uuid.uuid4().hex[:6]}",
        "bu": "retail",
        "display_name": "Retail KB library",
        "sp_site_id": "retail",
        "sp_drive_id": "drive-retail",
        "sp_folder_path": "/KB",
        "revision": {
            "classification": "INTERNAL",
            "retention_class": "R0D",  # synthetic class: purge as soon as a version is no longer current
            "acl_groups": ["GRP_KB_RETAIL_READ"],
            "allowed_extensions": ["pdf", "docx", "pptx", "xlsx"],
            "max_file_bytes": 25 * 1024 * 1024,
        },
    }
    r = api.call("POST", "/sources", "ret.maker", body)
    assert r.status_code == 201, r.text
    sid, rid = r.json()["source"]["source_id"], r.json()["revision"]["revision_id"]
    req = api.call("POST", f"/revisions/{rid}/submit", "ret.maker", {}).json()["request_id"]

    report = engine.dry_run(sid, rid)
    assert report["counts"]["items"] == 10 and report["counts"]["would_index"] == 10
    detail = api.call("GET", f"/requests/{req}", "ret.checker").json()
    assert detail["dry_run"]["status"] == "SUCCEEDED" and detail["dry_run"]["stats"]["counts"]["would_index"] == 10
    with pytest.raises(RuntimeError, match="DRAFT"):
        engine.sync(sid, "FIRST_LOAD")  # nothing loads before approval
    assert api.call("POST", f"/requests/{req}/approve", "ret.checker", {"reason": "dry run clean"}).status_code == 200

    # ---- 2. first load ----------------------------------------------------------------------------------
    stats = engine.sync(sid, "FIRST_LOAD")
    assert stats["counts"]["indexed_versions"] == 10, stats
    hp = item_doc(sid, "ret-home-loan-policy")
    assert visible_versions(engine, hp) == {f"{hp}:v1"}
    canary = engine.index.bm25("COPPERFINCH", flt("ret.user1"), 5)
    assert canary and "COPPERFINCH" in canary[0].chunk.text
    assert engine.index.bm25("COPPERFINCH", flt("corp.user1"), 5) == []  # pre-filter: other BU sees nothing
    assert engine.index.bm25("COPPERFINCH", flt("nobody"), 5) == []
    ocr = engine.index.bm25("QZ-5519-PLOVER hotline", flt("ret.user2"), 5)  # scanned PDF went through OCR
    assert any("PLOVER" in h.chunk.text for h in ocr)

    rerun = engine.sync(sid, "DELTA")  # idempotent: nothing changed
    assert rerun["counts"].get("indexed_versions", 0) == 0

    # ---- 3. edit -> new version current, old hidden --------------------------------------------------------
    scenarios.new_version(sp, "ret-home-loan-policy")
    stats = engine.sync(sid, "DELTA")
    assert stats["counts"]["indexed_versions"] == 1
    v = versions(as_admin, hp)
    assert [(x["version_no"], x["status"], x["is_current"]) for x in v] == [
        (1, "SUPERSEDED", False),
        (2, "INDEXED", True),
    ]
    assert visible_versions(engine, hp) == {f"{hp}:v2"}
    ltv = [h for h in engine.index.bm25("loan-to-value owner-occupied", flt("ret.user1"), 20) if h.chunk.doc_id == hp]
    assert ltv and all(h.chunk.version_id.endswith(":v2") for h in ltv) and "80%" in ltv[0].chunk.text

    # ---- 4. delete -> out of the index, raw copy kept ---------------------------------------------------------
    pl = item_doc(sid, "ret-personal-loan-proc")
    scenarios.delete(sp, "ret-personal-loan-proc")
    engine.sync(sid, "DELTA")
    (pv,) = versions(as_admin, pl)
    assert pv["status"] == "DELETED" and not pv["is_current"]
    assert visible_versions(engine, pl) == set()
    assert raw_store.exists(pv["raw_uri"]), "raw copy must be retained until its retention date"

    # ---- 5. legal hold blocks purge ----------------------------------------------------------------------------
    bo = item_doc(sid, "ret-branch-onboarding")
    scenarios.delete(sp, "ret-branch-onboarding")
    engine.sync(sid, "DELTA")
    hold = api.call("POST", "/legal-holds", "ret.checker", {"doc_id": bo, "reason": "litigation ref L-001 (synthetic)"})
    assert hold.status_code == 201, hold.text
    purge = engine.purge()
    assert purge["counts"] == {"held": 1, "purged": 2}, purge  # home-loan v1 + personal loan; onboarding held
    (bv,) = versions(as_admin, bo)
    assert bv["status"] == "DELETED" and raw_store.exists(bv["raw_uri"])
    assert not raw_store.exists(pv["raw_uri"]) and versions(as_admin, pl)[0]["status"] == "PURGED"
    with as_admin() as c:
        certs = c.execute(
            "SELECT version_id FROM kb.purge_certificate WHERE doc_id IN (%s, %s, %s)", (hp, pl, bo)
        ).fetchall()
        assert sorted(x["version_id"] for x in certs) == sorted([f"{hp}:v1", f"{pl}:v1"])
    with as_admin() as c, pytest.raises(pg.InsufficientPrivilege, match="legal hold"):
        c.execute(
            "INSERT INTO kb.purge_certificate (doc_id, version_id, raw_uri, sha256, retention_until, purged_by) "
            "VALUES (%s, %s, 'x', repeat('0', 64), current_date, 'test')",
            (bo, f"{bo}:v1"),
        )
    rel = api.call("POST", f"/legal-holds/{hold.json()['hold_id']}/release", "ret.maker", {"comment": "case closed"})
    assert api.call("POST", f"/requests/{rel.json()['request_id']}/approve", "ret.checker", {}).status_code == 200
    assert engine.purge()["counts"] == {"purged": 1}

    # ---- 6. PII exception: released by the checker, reprocessed ------------------------------------------------
    pii = scenarios.add_pii_heavy(sp, "retail")
    stats = engine.sync(sid, "DELTA")
    assert stats["counts"]["exception:PII_THRESHOLD"] == 1
    assert engine.sync(sid, "DELTA")["counts"].get("exception:PII_THRESHOLD", 0) == 0  # no duplicate exception
    pdoc = doc_id_for(sid, pii["item_id"])
    exc = [e for e in api.call("GET", "/exceptions", "ret.maker").json() if e["doc_id"] == pdoc]
    assert len(exc) == 1 and exc[0]["reason"] == "PII_THRESHOLD" and exc[0]["releasable"]
    assert visible_versions(engine, pdoc) == set()  # stopped at the gate: not searchable
    rr = api.call("POST", f"/exceptions/{exc[0]['exception_id']}/release", "ret.maker", {"comment": "synthetic"})
    assert rr.status_code == 201, rr.text
    assert api.call("POST", f"/requests/{rr.json()['request_id']}/approve", "ret.maker", {}).status_code == 403
    assert api.call("POST", f"/requests/{rr.json()['request_id']}/approve", "ret.checker", {}).status_code == 200
    out = engine.reprocess_released(exc[0]["exception_id"])
    assert out["counts"]["reprocessed_ok"] == 1
    (xv,) = versions(as_admin, pdoc)
    assert xv["status"] == "INDEXED" and xv["is_current"]
    assert visible_versions(engine, pdoc) == {f"{pdoc}:v1"}
    assert engine.reprocess_released(exc[0]["exception_id"]) == {"skipped": exc[0]["exception_id"]}

    # ---- 7. other exception kinds; only releasable ones can be released ----------------------------------------
    scenarios.add_oversized(sp, "retail")
    scenarios.add_unsupported(sp, "retail")
    scenarios.add_corrupt(sp, "retail")
    stats = engine.sync(sid, "DELTA")
    for reason in ("SIZE_LIMIT", "UNSUPPORTED_TYPE", "PARSE_FAILURE"):
        assert stats["counts"][f"exception:{reason}"] == 1, stats
    open_exc = {e["reason"]: e for e in api.call("GET", "/exceptions", "ret.maker").json() if e["source_id"] == sid}
    r = api.call("POST", f"/exceptions/{open_exc['SIZE_LIMIT']['exception_id']}/release", "ret.maker", {})
    assert r.status_code == 403 and "cannot be released" in r.text
    r = api.call(
        "POST",
        f"/exceptions/{open_exc['UNSUPPORTED_TYPE']['exception_id']}/exclude",
        "ret.checker",
        {"reason": "project files are out of scope"},
    )
    assert r.status_code == 200 and r.json()["status"] == "EXCLUDED"

    # ---- 8. move out of scope -> hidden ------------------------------------------------------------------------
    fees = item_doc(sid, "ret-fees")
    scenarios.move_out_of_scope(sp, "ret-fees")
    engine.sync(sid, "DELTA")
    assert visible_versions(engine, fees) == set()
    with as_admin() as c:
        assert (
            c.execute("SELECT status FROM kb.document WHERE doc_id = %s", (fees,)).fetchone()["status"]
            == "OUT_OF_SCOPE"
        )

    # ---- 9. expired token -> resync converges -------------------------------------------------------------------
    cc = item_doc(sid, "ret-credit-card-sheet")
    scenarios.new_version(sp, "ret-credit-card-sheet")
    scenarios.expire_tokens(sp)
    stats = engine.sync(sid, "DELTA")
    assert stats["counts"]["resync_required"] == 1 and stats["counts"]["indexed_versions"] == 1
    assert visible_versions(engine, cc) == {f"{cc}:v2"}
    assert visible_versions(engine, fees) == set() and visible_versions(engine, pl) == set()
    again = engine.sync(sid, "DELTA")
    assert again["counts"].get("indexed_versions", 0) == 0 and "resync_required" not in again["counts"]

    # ---- 10. one run per source ---------------------------------------------------------------------------------
    src = engine.source_ctx(sid)
    run_id = engine._start_run(src, "DELTA")
    with pytest.raises(RunConflictError):
        engine.sync(sid, "DELTA")
    engine._finish_run(run_id, "CANCELLED", type("S", (), {"as_json": lambda self: {}})())

    # ---- 11. registry and index agree for every live document ------------------------------------------------------
    with as_admin() as c:
        live = c.execute(
            "SELECT d.doc_id, v.version_id FROM kb.document d JOIN kb.document_version v ON v.doc_id = d.doc_id "
            "AND v.is_current WHERE d.source_id = %s AND d.status = 'ACTIVE'",
            (sid,),
        ).fetchall()
    current = {
        d["doc_id"]: {d["version_id"]}
        for d in engine.index.docs(
            {"bool": {"filter": [{"term": {"source_id": sid}}, {"term": {"is_current": True}}]}}, size=2000
        )
    }
    assert {r["doc_id"]: {r["version_id"]} for r in live} == current
