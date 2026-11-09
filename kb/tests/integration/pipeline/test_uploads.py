"""Phase 2 acceptance: every upload scenario from the synthetic manifest (CLAUDE.md section 6)."""

from __future__ import annotations

import io
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kb.adapters.local.es_index import render_filter
from kb.adapters.local.identity import YamlIdentityProvider
from kb.config import load_settings
from kb.gateway.app import create_app
from kb.ingest import work
from kb.ingest.engine import IngestEngine
from kb.ingest.factory import build_engine
from kb.ports.types import AccessFilter

from helpers import Api, onboard, uid

IDP = YamlIdentityProvider("config/users.yaml")
LIMIT = 2 * 1024 * 1024


@pytest.fixture(scope="module")
def env(tmp_path_factory, data_dir, pipe_dsn, registry, manifest):
    root = tmp_path_factory.mktemp("uploads")
    sp = root / "sharepoint"
    shutil.copytree(data_dir / "sharepoint", sp)
    engine = build_engine(
        load_settings("local", strict=True),
        registry_dsn=pipe_dsn,
        index_alias=f"kb-test-{uuid.uuid4().hex[:8]}",
        raw_url=(root / "raw").as_uri(),
        processed_url=(root / "processed").as_uri(),
        sharepoint_root=str(sp),
    )
    engine.index.ensure_index()
    app = create_app(
        load_settings("local"),
        registry,
        IDP,
        trusted_clients=frozenset({"testclient"}),
        raw_store=engine.raw,
        chunk_index=engine.index,
    )
    with TestClient(app) as client:
        yield {
            "engine": engine,
            "client": client,
            "root": root,
            "sp": sp,
            "fixtures": {u["scenario"]: u for u in manifest["upload_scenarios"]},
            "data": data_dir,
        }
    engine.index.drop()
    engine.reg.close()


@pytest.fixture(scope="module")
def spaces(env, client):
    api = Api(client)
    out = {}
    for bu in ("retail", "corporate", "wealth"):
        out[bu] = onboard(api, bu, "UPLOAD", max_file_bytes=LIMIT)["source_id"]
    return out


def put(env: dict[str, Any], user: str, space: str, path: Path, name: str | None = None, **params: Any) -> Any:
    with open(path, "rb") as f:
        return env["client"].post(
            f"/api/v1/uploads/{space}",
            headers={"X-KB-User": user},
            files={"file": (name or path.name, f)},
            params=params,
        )


def fixture_path(env, scenario: str) -> Path:
    return env["data"] / env["fixtures"][scenario]["file"]


def sees(engine: IngestEngine, user: str, doc_id: str) -> set[str]:
    flt = AccessFilter(frozenset(IDP.resolve(user).acl_principals))
    q = {"bool": {"filter": [*render_filter(flt), {"term": {"doc_id": doc_id}}]}}
    return {d["version_id"] for d in engine.index.docs(q)}


def secret_visible(engine: IngestEngine, user: str, secret: str) -> bool:
    flt = AccessFilter(frozenset(IDP.resolve(user).acl_principals))
    return any(secret in h.chunk.text for h in engine.index.bm25(secret, flt, 10))


def raw_files(env) -> int:
    raw = env["root"] / "raw"
    return sum(1 for p in raw.rglob("*") if p.is_file()) if raw.exists() else 0


# ---------------------------------------------------------------------------------------------- scenarios
def test_private_upload_visible_only_to_uploader(env, spaces, as_admin):
    eng = env["engine"]
    r = put(env, "ret.user1", spaces["retail"], fixture_path(env, "private_upload"))
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["status"] == "RECEIVED" and up["version_no"] == 1
    assert up["version_id"] in {w["version_id"] for w in work.received_uploads(eng)}  # sensor will pick it up
    assert eng.process_upload(up["version_id"])["counts"]["indexed_versions"] == 1
    assert sees(eng, "ret.user1", up["doc_id"]) == {up["version_id"]}
    for other in ("ret.user2", "ret.checker", "ret.maker", "corp.user1", "grp.reader", "nobody"):
        assert sees(eng, other, up["doc_id"]) == set(), other
    assert secret_visible(eng, "ret.user1", "TEALSPARROW") and not secret_visible(eng, "ret.user2", "TEALSPARROW")
    with as_admin() as c:  # audit: uploader, time, source IP, hash
        a = c.execute(
            "SELECT actor_id, details FROM kb.audit_log WHERE action = 'UPLOAD_RECEIVED' AND object_id = %s",
            (up["version_id"],),
        ).fetchone()
    assert a["actor_id"] == "ret.user1" and a["details"]["sha256"] == up["sha256"] and a["details"]["source_ip"]
    env["private"] = up


def test_reupload_as_new_version(env, spaces):
    eng, first = env["engine"], env["private"]
    same = put(env, "ret.user1", spaces["retail"], fixture_path(env, "private_upload"))
    assert same.status_code == 409 and first["doc_id"] in same.text  # own duplicate: points at the existing doc
    ident = put(
        env, "ret.user1", spaces["retail"], fixture_path(env, "private_upload"), as_new_version_of=first["doc_id"]
    )
    assert ident.status_code == 409  # identical content is not a new version
    r = put(
        env, "ret.user1", spaces["retail"], fixture_path(env, "reupload_new_version"), as_new_version_of=first["doc_id"]
    )
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["doc_id"] == first["doc_id"] and v2["version_no"] == 2
    assert (
        put(
            env,
            "ret.user2",
            spaces["retail"],
            fixture_path(env, "reupload_new_version"),
            name="x.docx",
            as_new_version_of=first["doc_id"],
        ).status_code
        == 403
    )  # not your document
    eng.process_upload(v2["version_id"])
    assert sees(eng, "ret.user1", first["doc_id"]) == {v2["version_id"]}
    mine = env["client"].get("/api/v1/uploads/mine", headers={"X-KB-User": "ret.user1"}).json()
    doc = next(d for d in mine if d["doc_id"] == first["doc_id"])
    assert [(v["version_no"], v["status"], v["is_current"]) for v in doc["versions"]] == [
        (1, "SUPERSEDED", False),
        (2, "INDEXED", True),
    ]


def test_publish_approved_restamps_without_reembedding(env, spaces, as_admin):
    eng, client = env["engine"], env["client"]
    up = put(env, "ret.user1", spaces["retail"], fixture_path(env, "publish_approved")).json()
    eng.process_upload(up["version_id"])
    assert not secret_visible(eng, "ret.user2", "MAGENTA-7")
    before = {d["chunk_id"]: d["indexed_at"] for d in eng.index.docs({"term": {"doc_id": up["doc_id"]}})}
    api = Api(client)
    req = api.call("POST", f"/documents/{up['doc_id']}/publish", "ret.user1", {"comment": "branch tips"}).json()
    assert work.index_drift(eng) == []  # nothing changes until a checker approves
    assert api.call("POST", f"/requests/{req['request_id']}/approve", "ret.checker", {}).status_code == 200
    assert up["doc_id"] in work.index_drift(eng)
    assert eng.index_sync()["counts"]["acl_restamped"] == 1 and work.index_drift(eng) == []
    after = {d["chunk_id"]: d["indexed_at"] for d in eng.index.docs({"term": {"doc_id": up["doc_id"]}})}
    assert after == before  # same chunks, same indexed_at: re-stamped, not re-embedded
    assert secret_visible(eng, "ret.user2", "MAGENTA-7") and secret_visible(eng, "ret.checker", "MAGENTA-7")
    assert not secret_visible(eng, "corp.user1", "MAGENTA-7") and not secret_visible(eng, "nobody", "MAGENTA-7")
    # unpublish (single actor) restricts again
    assert (
        api.call("POST", f"/documents/{up['doc_id']}/unpublish", "ret.user1", {"reason": "outdated"}).status_code == 200
    )
    eng.index_sync()
    assert not secret_visible(eng, "ret.user2", "MAGENTA-7") and secret_visible(eng, "ret.user1", "MAGENTA-7")


def test_publish_rejected_stays_private(env, spaces):
    eng, api = env["engine"], Api(env["client"])
    up = put(env, "corp.user1", spaces["corporate"], fixture_path(env, "publish_rejected")).json()
    eng.process_upload(up["version_id"])
    req = api.call("POST", f"/documents/{up['doc_id']}/publish", "corp.user1", {}).json()
    r = api.call("POST", f"/requests/{req['request_id']}/reject", "corp.checker", {"reason": "draft figures"})
    assert r.status_code == 200
    assert eng.index_sync()["counts"].get("acl_restamped", 0) == 0
    assert secret_visible(eng, "corp.user1", "RAVEN") and not secret_visible(eng, "corp.user2", "RAVEN")


def test_duplicate_of_sharepoint_is_linked_not_indexed(env, spaces, as_admin):
    eng, api = env["engine"], Api(env["client"])
    sp_src = onboard(api, "wealth", "SHAREPOINT", sp_site_id="wealth")
    eng.sync(sp_src["source_id"], "FIRST_LOAD")
    r = put(env, "wlth.user1", spaces["wealth"], fixture_path(env, "duplicate_of_sharepoint"))
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["status"] == "LINKED" and up["duplicate_of"]
    with as_admin() as c:
        sp_doc = c.execute(
            "SELECT source_id, title FROM kb.document WHERE doc_id = %s", (up["duplicate_of"],)
        ).fetchone()
    assert sp_doc["source_id"] == sp_src["source_id"]
    assert up["version_id"] not in {w["version_id"] for w in work.received_uploads(eng)}
    assert eng.index.count({"term": {"doc_id": up["doc_id"]}}) == 0  # not indexed twice


def test_lying_extension_rejected_and_uploader_notified(env, spaces):
    eng, client = env["engine"], env["client"]
    r = put(env, "corp.user2", spaces["corporate"], fixture_path(env, "lying_extension"))
    assert r.status_code == 201  # extension is allowed; the content check runs in the pipeline gates
    up = r.json()
    stats = eng.process_upload(up["version_id"])
    assert stats["counts"]["exception:TYPE_MISMATCH"] == 1
    assert eng.index.count({"term": {"doc_id": up["doc_id"]}}) == 0
    notes = client.get("/api/v1/uploads/notifications", headers={"X-KB-User": "corp.user2"}).json()
    assert any(n["kind"] == "UPLOAD_REJECTED" and "TYPE_MISMATCH" in n["message"] for n in notes)
    mine = client.get("/api/v1/uploads/mine", headers={"X-KB-User": "corp.user2"}).json()
    doc = next(d for d in mine if d["doc_id"] == up["doc_id"])
    assert doc["versions"][0]["status"] == "EXCEPTION" and doc["exceptions"][0]["reason"] == "TYPE_MISMATCH"
    others = client.get("/api/v1/uploads/notifications", headers={"X-KB-User": "corp.user1"}).json()
    assert not any(n["ref"].get("doc_id") == up["doc_id"] for n in others)  # only the uploader is told


def test_upload_to_forbidden_space_refused_and_nothing_stored(env, spaces):
    n = raw_files(env)
    r = put(env, "ret.user2", spaces["wealth"], fixture_path(env, "forbidden_space"))
    assert r.status_code == 403 and raw_files(env) == n
    assert env["client"].get("/api/v1/uploads/spaces", headers={"X-KB-User": "ret.user2"}).json()[0]["bu"] == "retail"
    assert put(env, "nobody", spaces["retail"], fixture_path(env, "forbidden_space")).status_code == 403


# ---------------------------------------------------------------------------------------------- gates and deletion
def test_oversized_upload_aborted_mid_stream(env, spaces):
    n = raw_files(env)
    big = io.BytesIO(b"%PDF-1.7\n" + b"0" * (LIMIT + 10))
    r = env["client"].post(
        f"/api/v1/uploads/{spaces['retail']}", headers={"X-KB-User": "ret.user1"}, files={"file": ("big.pdf", big)}
    )
    assert r.status_code == 413 and raw_files(env) == n  # no partial file left behind


def test_disallowed_extension_refused(env, spaces):
    r = env["client"].post(
        f"/api/v1/uploads/{spaces['retail']}",
        headers={"X-KB-User": "ret.user1"},
        files={"file": ("tool.exe", io.BytesIO(b"MZ..."))},
    )
    assert r.status_code == 415


def test_uploader_delete_is_immediate_and_raw_retained(env, spaces, as_admin):
    eng, client = env["engine"], env["client"]
    p = env["root"] / f"{uid('note')}.docx"
    shutil.copyfile(fixture_path(env, "forbidden_space"), p)  # a harmless docx
    up = put(env, "ret.user1", spaces["retail"], p).json()
    eng.process_upload(up["version_id"])
    assert sees(eng, "ret.user1", up["doc_id"])
    assert client.delete(f"/api/v1/uploads/{up['doc_id']}", headers={"X-KB-User": "ret.user2"}).status_code == 404
    assert client.delete(f"/api/v1/uploads/{up['doc_id']}", headers={"X-KB-User": "ret.user1"}).status_code == 200
    assert sees(eng, "ret.user1", up["doc_id"]) == set()  # immediately, before any sensor runs
    with as_admin() as c:
        raw_uri = c.execute(
            "SELECT raw_uri FROM kb.document_version WHERE version_id = %s", (up["version_id"],)
        ).fetchone()["raw_uri"]
    assert eng.raw.exists(raw_uri)
    eng.index_sync()  # the safety net closes the version and starts its retention clock
    with as_admin() as c:
        v = c.execute(
            "SELECT status, is_current, retention_until FROM kb.document_version WHERE version_id = %s",
            (up["version_id"],),
        ).fetchone()
    assert v["status"] == "DELETED" and not v["is_current"] and v["retention_until"]
