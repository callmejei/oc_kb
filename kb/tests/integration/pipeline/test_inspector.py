"""Phase 6 backend: document inspector, answer flags, re-parse, exclude / include, end to end on a real
index, raw store and Docling (retail /KB/Policies: one DOCX, one PDF).

Security properties proven here:
- the inspector shows a document only to callers whose index ACL lets at least one chunk through;
  everyone else gets 404 (no existence leak), including makers of other BUs;
- readers see the current version only; governance also sees superseded / excluded versions of its BU;
- a flag reaches only the cited BU's governance; exclude hides at once; include needs four eyes and
  restores with today's ACL.
"""

from __future__ import annotations

import shutil
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from kb.adapters.local.identity import YamlIdentityProvider
from kb.config import load_settings
from kb.gateway.acl import build_access_filter
from kb.gateway.app import create_app
from kb.ingest import work
from kb.ingest.engine import IngestEngine, doc_id_for
from kb.ingest.factory import build_engine
from synthetic import scenarios
from synthetic.sharepoint import SharePointSim

from helpers import Api

IDP = YamlIdentityProvider("config/users.yaml")


@pytest.fixture(scope="module")
def world(tmp_path_factory, data_dir, pipe_dsn, regdb, registry):
    root = tmp_path_factory.mktemp("inspector")
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
    app = create_app(
        settings=load_settings("local"),
        registry=registry,
        idp=IDP,
        trusted_clients=frozenset({"testclient"}),
        raw_store=engine.raw,
        chunk_index=engine.index,
        processed_store=engine.processed,
    )
    with TestClient(app) as client:
        yield {"sp": sp, "engine": engine, "client": client}
    engine.index.drop()
    engine.reg.close()


def call(world: dict[str, Any], method: str, path: str, user: str, json: Any = None) -> Any:
    return world["client"].request(method, "/api/v1" + path, headers={"X-KB-User": user}, json=json)


@pytest.fixture(scope="module")
def loaded(world, client):
    api = Api(client)
    engine: IngestEngine = world["engine"]
    body = {
        "source_id": f"ret-pol-{uuid.uuid4().hex[:6]}",
        "bu": "retail",
        "display_name": "Retail policies",
        "sp_site_id": "retail",
        "sp_drive_id": "drive-retail",
        "sp_folder_path": "/KB/Policies",
        "revision": {
            "classification": "INTERNAL",
            "retention_class": "R7Y",
            "acl_groups": ["GRP_KB_RETAIL_READ"],
            "allowed_extensions": ["pdf", "docx", "pptx", "xlsx"],
            "max_file_bytes": 25 * 1024 * 1024,
        },
    }
    r = api.call("POST", "/sources", "ret.maker", body)
    assert r.status_code == 201, r.text
    sid, rid = r.json()["source"]["source_id"], r.json()["revision"]["revision_id"]
    req = api.call("POST", f"/revisions/{rid}/submit", "ret.maker", {}).json()["request_id"]
    assert api.call("POST", f"/requests/{req}/approve", "ret.checker", {"reason": "ok"}).status_code == 200
    stats = engine.sync(sid, "FIRST_LOAD")
    assert stats["counts"]["indexed_versions"] == 2, stats
    return {
        "source_id": sid,
        "pdf": doc_id_for(sid, SharePointSim.item_id_for("ret-valuation-guide")),
        "docx": doc_id_for(sid, SharePointSim.item_id_for("ret-home-loan-policy")),
    }


def reader_versions(engine: IngestEngine, doc: str, user: str = "ret.user1") -> set[str]:
    return engine.index.visible_versions(doc, build_access_filter(IDP.resolve(user)))


# ------------------------------------------------------------------------------------------- reads
def test_reader_sees_pages_blocks_chunks_and_the_pdf(world, loaded):
    pdf = loaded["pdf"]
    d = call(world, "GET", f"/documents/{pdf}", "ret.user1")
    assert d.status_code == 200, d.text
    j = d.json()
    assert [v["version_no"] for v in j["versions"]] == [1] and j["governance"]["governs"] is False
    assert j["governance"]["labels"] is None  # governance detail only for governance
    ch = call(world, "GET", f"/documents/{pdf}/versions/1/chunks", "ret.user1").json()
    assert ch and any(it.get("bbox") for c in ch for it in c["doc_items"])
    bl = call(world, "GET", f"/documents/{pdf}/versions/1/blocks", "ret.user1").json()
    page1 = bl["pages"]["1"]
    boxes = [b for blk in bl["blocks"] for b in blk["boxes"]]
    assert boxes and all(0 <= b["bbox"][1] <= b["bbox"][3] <= page1["height"] + 1 for b in boxes if b["page"] == 1)
    assert any(blk["label"] == "table" and "|" in blk["text"] for blk in bl["blocks"])  # LTV table as markdown
    f = call(world, "GET", f"/documents/{pdf}/versions/1/file", "ret.user1")
    assert f.status_code == 200 and f.content[:4] == b"%PDF" and f.headers["content-type"] == "application/pdf"
    docx = call(world, "GET", f"/documents/{loaded['docx']}/versions/1/blocks", "ret.user1").json()
    assert docx["blocks"]  # Office files: structure without page images (D-046)


@pytest.mark.parametrize("user", ["corp.user1", "wlth.maker", "corp.checker", "nobody"])
def test_other_bus_and_no_groups_get_404_everywhere(world, loaded, user):
    pdf = loaded["pdf"]
    for path in ("", "/versions/1/chunks", "/versions/1/blocks", "/versions/1/file"):
        assert call(world, "GET", f"/documents/{pdf}{path}", user).status_code == 404, (user, path)
    for path, body in (
        ("/labels", {"version_no": 1, "problem": "OTHER"}),
        ("/reparse", {"reason": "x"}),
        ("/exclude", {"reason": "x"}),
        ("/include", {}),
    ):
        assert call(world, "POST", f"/documents/{pdf}{path}", user, body).status_code == 404, (user, path)


def test_readers_cannot_act(world, loaded):
    pdf = loaded["pdf"]
    assert call(world, "POST", f"/documents/{pdf}/reparse", "ret.user1", {"reason": "x"}).status_code == 403
    assert call(world, "POST", f"/documents/{pdf}/exclude", "ret.user1", {"reason": "x"}).status_code == 403
    r = call(world, "POST", f"/documents/{pdf}/labels", "ret.user1", {"version_no": 1, "problem": "OTHER"})
    assert r.status_code == 403


# ------------------------------------------------------------------------- flag -> inspector -> re-parse
def test_flag_reaches_only_the_cited_bus_governance(world, loaded, as_api):
    qid = str(uuid.uuid4())
    with as_api("ret.user1") as c:  # what AskService writes for an answer citing the PDF
        c.execute(
            "INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, details) "
            "VALUES (kb.current_actor(), 'QUERY', 'query', %s, %s)",
            (qid, Jsonb({"cited_doc_versions": [f"{loaded['pdf']}:v1"]})),
        )
    bad = {"query_id": qid, "rating": "DOWN", "problem": "TABLE_BROKEN", "reason": "x"}
    assert call(world, "POST", "/feedback", "ret.user2", bad).status_code == 404  # not their query
    r = call(world, "POST", "/feedback", "ret.user1", bad | {"reason": "LTV table columns shifted"})
    assert r.status_code == 201, r.text
    fid = r.json()["feedback_id"]
    mine = [f for f in api_gov(world, "GET", "/feedback", "ret.maker").json() if f["feedback_id"] == fid]
    assert mine and mine[0]["versions"][0]["doc_id"] == loaded["pdf"] and "question" not in mine[0]
    assert not [f for f in api_gov(world, "GET", "/feedback", "wlth.maker").json() if f["feedback_id"] == fid]
    assert api_gov(world, "GET", "/feedback", "ret.user1").status_code == 403
    world["feedback_id"] = fid


def api_gov(world, method, path, user, json=None):
    return call(world, method, "/governance" + path, user, json)


def test_reparse_creates_a_new_current_version_with_its_own_raw_copy(world, loaded, as_admin):
    engine: IngestEngine = world["engine"]
    pdf, fid = loaded["pdf"], world.get("feedback_id")
    lab = {"version_no": 1, "page": 1, "problem": "TABLE_BROKEN", "note": "columns shifted", "feedback_id": fid}
    assert call(world, "POST", f"/documents/{pdf}/labels", "ret.maker", lab).status_code == 201
    body = {
        "options": {"ocr_mode": "full_page", "table_cell_matching": False},
        "reason": "fix table",
        "feedback_id": fid,
    }
    assert call(world, "POST", f"/documents/{pdf}/reparse", "ret.checker", body).status_code == 403  # makers only
    r = call(world, "POST", f"/documents/{pdf}/reparse", "ret.maker", body)
    assert r.status_code == 201, r.text
    queued = [q["reparse_id"] for q in work.queued_reparses(engine)]
    assert str(r.json()["reparse_id"]) in queued
    stats = engine.reparse(str(r.json()["reparse_id"]))
    assert stats["counts"]["reparsed_ok"] == 1, stats
    with as_admin() as c:
        vs = c.execute(
            "SELECT version_no, status, is_current, raw_uri, processing FROM kb.document_version WHERE doc_id = %s "
            "ORDER BY version_no",
            (pdf,),
        ).fetchall()
        rq = c.execute("SELECT status, result_version_id FROM kb.reparse_request WHERE doc_id = %s", (pdf,)).fetchone()
    assert [(v["version_no"], v["status"], v["is_current"]) for v in vs] == [
        (1, "SUPERSEDED", False),
        (2, "INDEXED", True),
    ]
    assert vs[0]["raw_uri"] != vs[1]["raw_uri"] and vs[1]["processing"]["reparse_of"] == f"{pdf}:v1"
    assert vs[1]["processing"]["parse_options"] == {
        "ocr_mode": "full_page",
        "table_mode": "accurate",
        "table_cell_matching": "false",
    }
    assert rq == {"status": "DONE", "result_version_id": f"{pdf}:v2"}
    assert reader_versions(engine, pdf) == {f"{pdf}:v2"}  # readers: current only
    gov = call(world, "GET", f"/documents/{pdf}", "ret.maker").json()
    assert [v["version_no"] for v in gov["versions"]] == [1, 2] and gov["governance"]["labels"]  # governance: both
    assert call(world, "GET", f"/documents/{pdf}/versions/1/chunks", "ret.user1").status_code == 404
    assert call(world, "GET", f"/documents/{pdf}/versions/1/chunks", "ret.maker").status_code == 200
    res = api_gov(
        world, "POST", f"/feedback/{fid}/resolve", "ret.maker", {"bu": "retail", "resolution": "re-parsed v2"}
    )
    assert res.status_code == 200 and res.json()["status"] == "RESOLVED"


# ------------------------------------------------------------------------------------------- exclude / include
def test_exclude_hides_at_once_and_include_restores_after_four_eyes(world, loaded, as_admin):
    engine: IngestEngine = world["engine"]
    docx = loaded["docx"]
    assert reader_versions(engine, docx) == {f"{docx}:v1"}
    r = call(world, "POST", f"/documents/{docx}/exclude", "ret.checker", {"reason": "superseded by circular"})
    assert r.status_code == 200, r.text
    assert reader_versions(engine, docx) == set()  # out of search immediately
    assert call(world, "GET", f"/documents/{docx}", "ret.user1").status_code == 404
    assert call(world, "GET", f"/documents/{docx}", "ret.maker").status_code == 200  # governance still inspects
    engine.index_sync()  # sensor: closes the registry version
    # a later edit in SharePoint does not bring it back
    scenarios.new_version(world["sp"], "ret-home-loan-policy")
    stats = engine.sync(loaded["source_id"], "DELTA")
    assert stats["counts"].get("excluded_skipped") == 1 and reader_versions(engine, docx) == set(), stats
    # include: maker requests, the same maker cannot approve, a checker does
    inc = call(world, "POST", f"/documents/{docx}/include", "ret.maker", {"comment": "circular withdrawn"})
    assert inc.status_code == 201, inc.text
    req = inc.json()["request_id"]
    assert api_gov(world, "POST", f"/requests/{req}/approve", "ret.maker", {}).status_code == 403
    assert reader_versions(engine, docx) == set()
    assert api_gov(world, "POST", f"/requests/{req}/approve", "ret.checker", {"reason": "ok"}).status_code == 200
    assert reader_versions(engine, docx) == set()  # nothing visible until the pipeline restores
    assert docx in work.index_drift(engine)
    engine.index_sync()
    assert reader_versions(engine, docx) == {f"{docx}:v1"}
    with as_admin() as c:
        d = c.execute("SELECT status, restore_pending FROM kb.document WHERE doc_id = %s", (docx,)).fetchone()
    assert d == {"status": "ACTIVE", "restore_pending": False}
    stats = engine.sync(loaded["source_id"], "DELTA")  # the edit made while excluded now arrives as v2
    assert stats["counts"].get("indexed_versions") == 1 and reader_versions(engine, docx) == {f"{docx}:v2"}, stats
