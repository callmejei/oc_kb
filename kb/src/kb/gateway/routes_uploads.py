"""Uploads (CLAUDE.md section 5): receive, list "My files", delete, notifications.

The file is streamed in 1 MiB blocks from the request's spooled temp file into the raw store, hashed on
the way, and aborted as soon as it passes the space's size limit; it is never held in memory whole.
Gates 1-2 (allowed extension, size) run here and again in kb.register_upload; gates 3-4 (content type /
malware scan, PII) run in the pipeline, which picks up RECEIVED uploads within a minute.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, BinaryIO

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status

from kb.gateway.deps import GatewayState, current_identity, db_errors, state
from kb.ports.types import Identity

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]

SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


class TooLargeError(Exception):
    pass


class LimitedReader:
    """Wraps a binary stream; raises once more than ``limit`` bytes have been read."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self.raw, self.limit, self.read_bytes = raw, limit, 0

    def read(self, n: int = -1) -> bytes:
        block = self.raw.read(n)
        self.read_bytes += len(block)
        if self.read_bytes > self.limit:
            raise TooLargeError
        return block


def safe_filename(name: str | None) -> str:
    base = (name or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    clean = SAFE_NAME.sub("_", base).strip(" .")[:150]
    return clean or "upload"


def _space(st: GatewayState, ident: Identity, space: str) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as conn:
        row = conn.execute(
            """SELECT s.source_id, s.bu, s.status, s.source_type, r.acl_groups, r.max_file_bytes,
                      r.allowed_extensions
               FROM kb.kb_source s JOIN kb.source_revision r ON r.revision_id = s.current_revision_id
               WHERE s.source_id = %s""",
            (space,),
        ).fetchone()
    if row is None or row["source_type"] != "UPLOAD":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown upload space {space}")
    return row


@router.get("/spaces")
def my_spaces(ident: Ident, st: St) -> Any:
    """Approved upload spaces the caller may upload to (their groups intersect the space ACL)."""
    with st.registry.transaction(ident.user_id) as conn:
        rows = conn.execute(
            """SELECT s.source_id, s.bu, s.display_name, r.acl_groups, r.max_file_bytes, r.allowed_extensions
               FROM kb.kb_source s JOIN kb.source_revision r ON r.revision_id = s.current_revision_id
               WHERE s.source_type = 'UPLOAD' AND s.status = 'ACTIVE' ORDER BY s.source_id"""
        ).fetchall()
    return [r for r in rows if set(r["acl_groups"]) & ident.groups]


@router.post("/{space}", status_code=status.HTTP_201_CREATED)
def upload(
    space: str,
    request: Request,
    ident: Ident,
    st: St,
    file: Annotated[UploadFile, File()],
    as_new_version_of: Annotated[str | None, Query(max_length=80)] = None,
) -> Any:
    sp = _space(st, ident, space)
    # D-022: the live IdentityProvider decides who may upload to a space (not a DB copy of memberships)
    if not set(sp["acl_groups"]) & ident.groups:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"you have no access to upload space {space}")
    if sp["status"] != "ACTIVE":
        raise HTTPException(status.HTTP_409_CONFLICT, f"upload space {space} is {sp['status']}")
    name = safe_filename(file.filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in sp["allowed_extensions"]:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f".{ext} is not allowed in {space}")
    key = f"uploads/{sp['bu']}/{space}/{uuid.uuid4().hex}/{name}"
    try:
        stored = st.raw_store.put_stream(key, LimitedReader(file.file, int(sp["max_file_bytes"])))  # type: ignore[arg-type]
    except TooLargeError:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"file exceeds {sp['max_file_bytes']} bytes"
        ) from None
    client_ip = request.client.host if request.client else None
    try:
        with st.registry.transaction(ident.user_id) as conn, db_errors():
            row = conn.execute(
                "SELECT kb.register_upload(%s, %s, %s, %s, %s, %s::inet, %s) AS r",
                (
                    space,
                    name,
                    stored.sha256,
                    stored.size_bytes,
                    stored.uri,
                    client_ip if client_ip and client_ip != "testclient" else "127.0.0.1",
                    as_new_version_of,
                ),
            ).fetchone()
    except BaseException:
        st.raw_store.delete(stored.uri)  # refused: keep nothing
        raise
    assert row is not None
    return row["r"] | {"sha256": stored.sha256, "size_bytes": stored.size_bytes}


@router.get("/mine")
def my_files(ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn:
        return conn.execute(
            """SELECT d.doc_id, d.title, d.source_id, d.visibility, d.status, d.duplicate_of_doc_id,
                      d.current_version_no,
                      (SELECT json_agg(json_build_object('version_no', v.version_no, 'status', v.status,
                              'is_current', v.is_current, 'sha256', v.sha256, 'size_bytes', v.size_bytes,
                              'received_at', v.created_at) ORDER BY v.version_no)
                         FROM kb.document_version v WHERE v.doc_id = d.doc_id) AS versions,
                      (SELECT json_agg(json_build_object('reason', e.reason, 'status', e.status,
                              'detail', e.detail)) FROM kb.exception_item e WHERE e.doc_id = d.doc_id) AS exceptions,
                      (SELECT request_id FROM kb.approval_request a WHERE a.doc_id = d.doc_id
                         AND a.status = 'PENDING') AS pending_publish_request
               FROM kb.document d
               WHERE d.origin = 'UPLOAD' AND d.uploaded_by = %s AND d.status <> 'DELETED'
               ORDER BY d.first_seen_at DESC""",
            (ident.user_id,),
        ).fetchall()


@router.delete("/{doc_id}")
def delete_upload(doc_id: str, ident: Ident, st: St) -> Any:
    """Uploader deletes: out of search immediately (index first), then the registry. Raw copy is retained
    until its retention date (purge job, legal hold respected)."""
    with st.registry.transaction(ident.user_id) as conn:
        d = conn.execute(
            "SELECT uploaded_by, status FROM kb.document WHERE doc_id = %s AND origin = 'UPLOAD'", (doc_id,)
        ).fetchone()
    if d is None or d["uploaded_by"] != ident.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown upload {doc_id}")
    st.chunk_index.set_visibility(doc_id, current_version_id=None)
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        conn.execute("SELECT kb.delete_upload(%s)", (doc_id,))
    return {"doc_id": doc_id, "status": "DELETED"}


@router.get("/notifications")
def notifications(ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn:
        return conn.execute(
            "SELECT * FROM kb.notification WHERE user_id = %s ORDER BY created_at DESC LIMIT 100", (ident.user_id,)
        ).fetchall()
