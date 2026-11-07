"""Plain helpers for registry tests (importable; conftest is not)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

API = "/api/v1/governance"


@contextmanager
def owner_session(regdb: dict[str, Any], actor: str = "test:setup") -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Admin acting as kb_owner: for test *setup* only (e.g. extra governance roles)."""
    with psycopg.connect(**regdb, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('kb.actor_id', %s, false)", (actor,))
        conn.execute("SET ROLE kb_owner")
        yield conn


class Api:
    """Small helper: calls the governance API as a given user."""

    def __init__(self, client: TestClient) -> None:
        self.c = client

    def call(self, method: str, path: str, user: str, json: Any = None) -> Any:
        return self.c.request(method, API + path, headers={"X-KB-User": user}, json=json)


def uid(prefix: str = "t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def revision_body(bu: str = "retail", **over: Any) -> dict[str, Any]:
    acl = {
        "retail": ["GRP_KB_RETAIL_READ"],
        "corporate": ["GRP_KB_CORPORATE_READ"],
        "wealth": ["GRP_KB_WEALTH_READ"],
        "shared": ["GRP_KB_SHARED_READ"],
    }[bu]
    return {
        "classification": "INTERNAL",
        "retention_class": "R7Y",
        "acl_groups": acl,
        "allowed_extensions": ["pdf", "docx", "pptx", "xlsx"],
        "max_file_bytes": 25 * 1024 * 1024,
        **over,
    }


MAKER = {"retail": "ret.maker", "corporate": "corp.maker", "wealth": "wlth.maker", "shared": "shr.maker"}
CHECKER = {"retail": "ret.checker", "corporate": "corp.checker", "wealth": "wlth.checker", "shared": "shr.checker"}


def register(api: Api, bu: str = "retail", source_type: str = "SHAREPOINT", **rev: Any) -> dict[str, Any]:
    sp_over = {k: rev.pop(k) for k in list(rev) if k.startswith("sp_")}  # source fields, not revision fields
    body: dict[str, Any] = {
        "source_id": uid(bu[:3]),
        "bu": bu,
        "display_name": f"{bu} test source",
        "source_type": source_type,
        "revision": revision_body(bu, **rev),
    }
    if source_type == "SHAREPOINT":
        body |= {"sp_site_id": "site-1", "sp_drive_id": "drive-1", "sp_folder_path": "/KB"} | sp_over
    r = api.call("POST", "/sources", MAKER[bu], body)
    assert r.status_code == 201, r.text
    return r.json()


def onboard(api: Api, bu: str = "retail", source_type: str = "SHAREPOINT", **rev: Any) -> dict[str, Any]:
    """Register, submit and approve: returns the ACTIVE source."""
    created = register(api, bu, source_type, **rev)
    req = api.call("POST", f"/revisions/{created['revision']['revision_id']}/submit", MAKER[bu], {}).json()
    r = api.call("POST", f"/requests/{req['request_id']}/approve", CHECKER[bu], {"reason": "ok"})
    assert r.status_code == 200, r.text
    return {
        "source_id": created["source"]["source_id"],
        "revision_id": created["revision"]["revision_id"],
        "request_id": req["request_id"],
    }


def insert_upload(pipe_conn: psycopg.Connection[Any], source_id: str, uploader: str, ip: str = "127.0.0.1") -> str:
    """What Phase 2's upload path will do: register a RECEIVED private upload as the pipeline role."""
    doc_id = uid("doc")
    pipe_conn.execute(
        """INSERT INTO kb.document (doc_id, source_id, external_item_id, current_path, title, origin,
                                    uploaded_by, visibility)
           VALUES (%s, %s, %s, %s, 'Test upload', 'UPLOAD', %s, 'PRIVATE')""",
        (doc_id, source_id, doc_id, f"uploads/{doc_id}.pdf", uploader),
    )
    pipe_conn.execute(
        """INSERT INTO kb.document_version (version_id, doc_id, version_no, sha256, size_bytes, raw_uri,
                                            uploaded_by, source_ip, original_filename)
           VALUES (%s, %s, 1, %s, 1234, %s, %s, %s, 'test.pdf')""",
        (f"{doc_id}:v1", doc_id, "a" * 64, f"file:///raw/{doc_id}", uploader, ip),
    )
    return doc_id


class RecordingLLM:
    """Deterministic stand-in: records what the model would have seen; cites the first source."""

    def __init__(self, reply: str = "Per the documents [1].") -> None:
        self.reply = reply
        self.calls: list[Any] = []

    def chat(self, messages, *, temperature=0.0, max_tokens=None):  # type: ignore[no-untyped-def]
        self.calls.append(messages)
        return self.reply
