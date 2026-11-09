"""Document inspector (Phase 6): versions, Docling blocks with page boxes, chunks, the raw file for pdf.js,
and the governance actions that feed the pipeline (labels, re-parse, exclude, include).

Access has one authority, the index ACL. A caller may open a version only if at least one of its chunks
passes their filter:
- everyone: the mandatory retrieval filter (their groups, current versions, active sources);
- MAKER/CHECKER: additionally the inspection filter (still their groups, only BUs they govern, but also
  superseded / excluded versions).
A document no filter lets through is "unknown" (404), never "forbidden": no existence leak.
Every action is decided again in the database (migration 007).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from kb.gateway.acl import build_access_filter, build_inspection_filter
from kb.gateway.deps import GatewayState, current_identity, db_errors, roles, state
from kb.ports.types import AccessFilter, Identity

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]

MEDIA = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
Problem = Literal[
    "WRONG_ANSWER",
    "INCOMPLETE",
    "WRONG_CITATION",
    "OUTDATED",
    "TABLE_BROKEN",
    "OCR_ERROR",
    "READING_ORDER",
    "MISSING_TEXT",
    "WRONG_HEADING",
    "OTHER",
]


def governed_bus(st: GatewayState, ident: Identity) -> set[str]:
    return set(roles(st, ident, "MAKER")) | set(roles(st, ident, "CHECKER"))


def visible_versions(st: GatewayState, ident: Identity, doc_id: str) -> dict[str, AccessFilter]:
    """version_id -> the filter that lets the caller see it. Empty dict -> 404."""
    reader = build_access_filter(ident)
    out = dict.fromkeys(st.chunk_index.visible_versions(doc_id, reader), reader)
    gov = governed_bus(st, ident)
    if gov:
        insp = build_inspection_filter(ident, gov)
        for v in st.chunk_index.visible_versions(doc_id, insp):
            out.setdefault(v, insp)
    return out


def _require_visible(st: GatewayState, ident: Identity, doc_id: str, version_no: int | None = None) -> Any:
    vis = visible_versions(st, ident, doc_id)
    if not vis:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown document {doc_id}")
    if version_no is None:
        return vis
    vid = f"{doc_id}:v{version_no}"
    if vid not in vis:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown version {vid}")
    return vid, vis[vid]


# ------------------------------------------------------------------------------------------- reads
@router.get("/{doc_id}")
def document(doc_id: str, ident: Ident, st: St) -> Any:
    vis = _require_visible(st, ident, doc_id)
    with st.registry.transaction(ident.user_id) as conn:
        doc = conn.execute(
            """SELECT d.doc_id, d.title, d.current_path, d.status, d.origin, d.visibility, d.uploaded_by,
                      d.current_version_no, d.source_id, s.bu, s.display_name AS source_name
               FROM kb.document d JOIN kb.kb_source s USING (source_id) WHERE d.doc_id = %s""",
            (doc_id,),
        ).fetchone()
        versions = conn.execute(
            """SELECT version_no, version_id, status, is_current, chunk_count, mime_type, original_filename,
                      created_at, indexed_at, processing -> 'parse_options' AS parse_options,
                      processing -> 'reparse_of' AS reparse_of, processing -> 'pages' AS pages
               FROM kb.document_version WHERE doc_id = %s AND version_id = ANY(%s) ORDER BY version_no""",
            (doc_id, list(vis)),
        ).fetchall()
        governs = (
            doc is not None
            and doc["bu"] in governed_bus(st, ident)
            and not (doc["origin"] == "UPLOAD" and doc["visibility"] != "PUBLISHED")
        )
        labels = reparses = pending_include = None
        if governs:
            labels = conn.execute(
                "SELECT * FROM kb.inspection_label WHERE doc_id = %s ORDER BY created_at DESC", (doc_id,)
            ).fetchall()
            reparses = conn.execute(
                "SELECT * FROM kb.reparse_request WHERE doc_id = %s ORDER BY created_at DESC", (doc_id,)
            ).fetchall()
            row = conn.execute(
                """SELECT request_id FROM kb.approval_request WHERE doc_id = %s AND status = 'PENDING'
                   AND request_type = 'INCLUDE_DOCUMENT'""",
                (doc_id,),
            ).fetchone()
            pending_include = row["request_id"] if row else None
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown document {doc_id}")
    return {
        "document": doc,
        "versions": versions,
        "governance": {
            "governs": governs,
            "is_maker": governs and doc["bu"] in roles(st, ident, "MAKER"),
            "labels": labels,
            "reparses": reparses,
            "pending_include_request": pending_include,
        },
    }


@router.get("/{doc_id}/versions/{version_no}/chunks")
def chunks(doc_id: str, version_no: int, ident: Ident, st: St) -> Any:
    vid, flt = _require_visible(st, ident, doc_id, version_no)
    return [
        {
            "chunk_id": c.chunk.chunk_id,
            "chunk_no": c.chunk.metadata.get("chunk_no"),
            "text": c.chunk.text,
            "page": c.chunk.page,
            "page_end": c.chunk.metadata.get("page_end"),
            "section_path": c.chunk.section_path,
            "doc_items": (c.chunk.metadata.get("doc_items") or {}).get("items", []),
            "concept_ids": list(c.chunk.concept_ids),
        }
        for c in st.chunk_index.version_chunks(vid, flt)
    ]


@router.get("/{doc_id}/versions/{version_no}/blocks")
def blocks(doc_id: str, version_no: int, ident: Ident, st: St) -> Any:
    """Docling items in reading order, boxes converted to top-left page coordinates (PDF points)."""
    from docling_core.types.doc.document import DoclingDocument
    from docling_core.types.doc.items.table.table import TableItem

    vid, _ = _require_visible(st, ident, doc_id, version_no)
    key = f"{vid.replace(':', '_')}/docling.json"
    uri = st.processed_store.uri_for(key)
    if not st.processed_store.exists(uri):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no parse output for {vid}")
    with st.processed_store.open(uri) as f:
        doc = DoclingDocument.model_validate(json.load(f))
    pages = {int(n): {"width": p.size.width, "height": p.size.height} for n, p in doc.pages.items()}
    out = []
    for item, level in doc.iterate_items():
        text = item.export_to_markdown(doc) if isinstance(item, TableItem) else getattr(item, "text", "")
        boxes = []
        for prov in getattr(item, "prov", []) or []:
            h = pages.get(prov.page_no, {}).get("height")
            b = prov.bbox.to_top_left_origin(h) if h else prov.bbox
            boxes.append({"page": prov.page_no, "bbox": [round(b.l, 1), round(b.t, 1), round(b.r, 1), round(b.b, 1)]})
        out.append(
            {
                "ref": item.self_ref,
                "label": str(getattr(item, "label", "")),
                "level": level,
                "text": text[:4000],
                "boxes": boxes,
            }
        )
    return {"version_id": vid, "pages": pages, "blocks": out}


@router.get("/{doc_id}/versions/{version_no}/file")
def raw_file(doc_id: str, version_no: int, ident: Ident, st: St) -> StreamingResponse:
    vid, _ = _require_visible(st, ident, doc_id, version_no)
    with st.registry.transaction(ident.user_id) as conn:
        v = conn.execute(
            "SELECT raw_uri, original_filename, status FROM kb.document_version WHERE version_id = %s", (vid,)
        ).fetchone()
    if v is None or v["status"] == "PURGED" or not st.raw_store.exists(v["raw_uri"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"raw copy of {vid} is not available")
    name = v["original_filename"] or PurePosixPath(v["raw_uri"]).name
    ext = PurePosixPath(name).suffix.lower().lstrip(".")

    def stream() -> Iterator[bytes]:
        with st.raw_store.open(v["raw_uri"]) as f:
            while block := f.read(1 << 20):
                yield block

    return StreamingResponse(
        stream(),
        media_type=MEDIA.get(ext, "application/octet-stream"),
        headers={
            "Content-Disposition": f'{"inline" if ext == "pdf" else "attachment"}; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# ------------------------------------------------------------------------------------------- actions
class LabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_no: int = Field(gt=0)
    page: int | None = Field(default=None, gt=0)
    item_ref: str | None = Field(default=None, max_length=200)
    problem: Problem
    note: str | None = Field(default=None, max_length=2000)
    feedback_id: str | None = None


class ReparseOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ocr_mode: Literal["default", "full_page"] = "default"
    table_mode: Literal["accurate", "fast"] = "accurate"
    table_cell_matching: bool = True


class ReparseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: ReparseOptions = Field(default_factory=ReparseOptions)
    reason: str = Field(min_length=1, max_length=2000)
    feedback_id: str | None = None


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/{doc_id}/labels", status_code=status.HTTP_201_CREATED)
def add_label(doc_id: str, body: LabelBody, ident: Ident, st: St) -> Any:
    vid, _ = _require_visible(st, ident, doc_id, body.version_no)
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        row = conn.execute(
            "SELECT kb.add_inspection_label(%s, %s, %s, %s, %s, %s) AS id",
            (vid, body.page, body.item_ref, body.problem, body.note, body.feedback_id),
        ).fetchone()
    assert row is not None
    return {"label_id": row["id"]}


@router.post("/{doc_id}/reparse", status_code=status.HTTP_201_CREATED)
def request_reparse(doc_id: str, body: ReparseBody, ident: Ident, st: St) -> Any:
    _require_visible(st, ident, doc_id)
    opts = {k: str(v).lower() if isinstance(v, bool) else v for k, v in body.options.model_dump().items()}
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        row = conn.execute(
            "SELECT kb.request_reparse(%s, %s, %s, %s) AS id", (doc_id, Jsonb(opts), body.reason, body.feedback_id)
        ).fetchone()
    assert row is not None
    return {"reparse_id": row["id"], "status": "QUEUED"}


@router.post("/{doc_id}/exclude")
def exclude(doc_id: str, body: ReasonBody, ident: Ident, st: St) -> Any:
    """Restricts access, so one actor suffices. The database decides first (an unauthorised caller can
    never hide a document); then the index hides it at once. The index-sync sensor converges the rest."""
    _require_visible(st, ident, doc_id)
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        row = conn.execute("SELECT kb.exclude_document(%s, %s) AS id", (doc_id, body.reason)).fetchone()
    assert row is not None
    st.chunk_index.set_visibility(doc_id, current_version_id=None)
    return {"request_id": row["id"], "status": "EXCLUDED"}


@router.post("/{doc_id}/include", status_code=status.HTTP_201_CREATED)
def request_include(doc_id: str, body: CommentBody, ident: Ident, st: St) -> Any:
    """Re-including widens access again: a MAKER requests, a different CHECKER approves (four eyes)."""
    _require_visible(st, ident, doc_id)
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        row = conn.execute("SELECT kb.request_include(%s, %s) AS id", (doc_id, body.comment)).fetchone()
    assert row is not None
    return {"request_id": row["id"]}
