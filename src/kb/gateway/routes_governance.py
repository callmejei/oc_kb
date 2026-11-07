"""Governance API: makers (sources, revisions, submit, withdraw), checkers (inbox, decide), publish.

The API pre-checks roles for clear errors, but every rule is enforced again in the database; a
request that slips past the API is still refused there.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kb.gateway.deps import GatewayState, current_identity, db_errors, roles, state
from kb.ports.types import Identity
from kb.registry import repository as repo

router = APIRouter()

Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")


class RevisionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
    retention_class: str = Field(min_length=1, max_length=32)
    acl_groups: list[str] = Field(min_length=1)
    allowed_extensions: list[str] = Field(min_length=1)
    max_file_bytes: int = Field(gt=0, le=2 * 1024**3)
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    pii_policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_extensions")
    @classmethod
    def _ext(cls, v: list[str]) -> list[str]:
        return sorted({e.lower().lstrip(".") for e in v})


class RevisionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"] | None = None
    retention_class: str | None = Field(default=None, min_length=1, max_length=32)
    acl_groups: list[str] | None = Field(default=None, min_length=1)
    allowed_extensions: list[str] | None = Field(default=None, min_length=1)
    max_file_bytes: int | None = Field(default=None, gt=0, le=2 * 1024**3)
    include_paths: list[str] | None = None
    exclude_paths: list[str] | None = None
    pii_policy: dict[str, Any] | None = None


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    bu: str
    display_name: str = Field(min_length=1, max_length=200)
    source_type: Literal["SHAREPOINT", "UPLOAD"] = "SHAREPOINT"
    sp_site_id: str | None = None
    sp_drive_id: str | None = None
    sp_folder_path: str | None = None
    revision: RevisionFields

    @field_validator("source_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("source_id must be a lowercase slug (3-63 chars: a-z 0-9 _ -)")
        return v


class Comment(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class Reason(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class RequiredReason(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------- helpers
def _require(bus: list[str], bu: str, what: str) -> None:
    if bu not in bus:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"you are not a {what} for BU {bu}")


def _check_acl_groups(st: GatewayState, bu: str, groups: list[str]) -> None:
    """A source's ACL may only name document groups of its own BU (no cross-BU grants)."""
    bad = [
        g
        for g in groups
        if st.idp.groups.get(g, {}).get("bu") != bu or st.idp.groups[g]["kind"] not in ("read", "maker", "checker")
    ]
    if bad:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"acl_groups not valid for BU {bu}: {bad}")


def _source_bu(st: GatewayState, ident: Identity, source_id: str) -> str:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return str(repo.get_source(conn, source_id)["bu"])


# ---------------------------------------------------------------------------- maker
@router.get("/sources")
def list_sources(ident: Ident, st: St) -> Any:
    bus = sorted(set(roles(st, ident, "MAKER")) | set(roles(st, ident, "CHECKER")))
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return repo.list_sources(conn, bus)


@router.post("/sources", status_code=status.HTTP_201_CREATED)
def register_source(body: SourceCreate, ident: Ident, st: St) -> Any:
    _require(roles(st, ident, "MAKER"), body.bu, "MAKER")
    _check_acl_groups(st, body.bu, body.revision.acl_groups)
    src = body.model_dump(exclude={"revision"})
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return repo.create_source(conn, src, body.revision.model_dump())


@router.post("/sources/{source_id}/revisions", status_code=status.HTTP_201_CREATED)
def create_revision(source_id: str, body: RevisionPatch, ident: Ident, st: St) -> Any:
    bu = _source_bu(st, ident, source_id)
    _require(roles(st, ident, "MAKER"), bu, "MAKER")
    changes = body.model_dump(exclude_none=True)
    if "acl_groups" in changes:
        _check_acl_groups(st, bu, changes["acl_groups"])
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return repo.create_revision(conn, source_id, changes)


@router.patch("/revisions/{revision_id}")
def edit_revision(revision_id: str, body: RevisionPatch, ident: Ident, st: St) -> Any:
    changes = body.model_dump(exclude_none=True)
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        rev = repo.get_revision(conn, revision_id)
        bu = repo.get_source(conn, rev["source_id"])["bu"]
        _require(roles(st, ident, "MAKER"), bu, "MAKER")
        if "acl_groups" in changes:
            _check_acl_groups(st, bu, changes["acl_groups"])
        return repo.update_revision(conn, revision_id, changes)


@router.post("/revisions/{revision_id}/submit", status_code=status.HTTP_201_CREATED)
def submit_revision(revision_id: str, body: Comment, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return {"request_id": repo.submit_revision(conn, revision_id, body.comment)}


@router.post("/requests/{request_id}/withdraw")
def withdraw(request_id: str, body: Reason, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        repo.withdraw(conn, request_id, body.reason)
        return repo.request_detail(conn, request_id)["request"]


# ---------------------------------------------------------------------------- checker
@router.get("/inbox")
def inbox(ident: Ident, st: St) -> Any:
    bus = roles(st, ident, "CHECKER")
    if not bus:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not a CHECKER for any BU")
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return repo.inbox(conn, ident.user_id, bus)


@router.get("/requests/{request_id}")
def request_detail(request_id: str, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        detail = repo.request_detail(conn, request_id)
    allowed = set(roles(st, ident, "CHECKER")) | set(roles(st, ident, "MAKER"))
    if detail["request"]["maker_id"] != ident.user_id and detail["source"]["bu"] not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown request {request_id}")
    return detail


@router.post("/requests/{request_id}/approve")
def approve(request_id: str, body: Reason, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        repo.decide(conn, request_id, True, body.reason)
        return repo.request_detail(conn, request_id)["request"]


@router.post("/requests/{request_id}/reject")
def reject(request_id: str, body: RequiredReason, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        repo.decide(conn, request_id, False, body.reason)
        return repo.request_detail(conn, request_id)["request"]


# ---------------------------------------------------------------------------- uploads: publish
@router.post("/documents/{doc_id}/publish", status_code=status.HTTP_201_CREATED)
def publish(doc_id: str, body: Comment, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return {"request_id": repo.request_publish(conn, doc_id, body.comment)}


@router.post("/documents/{doc_id}/unpublish")
def unpublish(doc_id: str, body: RequiredReason, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return {"request_id": repo.unpublish(conn, doc_id, body.reason)}


# ---------------------------------------------------------------------------- exceptions
@router.get("/exceptions")
def list_exceptions(ident: Ident, st: St, status_filter: str | None = "OPEN") -> Any:
    bus = sorted(set(roles(st, ident, "MAKER")) | set(roles(st, ident, "CHECKER")))
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return repo.list_exceptions(conn, bus, status_filter)


@router.post("/exceptions/{exception_id}/release", status_code=status.HTTP_201_CREATED)
def request_release(exception_id: str, body: Comment, ident: Ident, st: St) -> Any:
    """Maker asks for a released reprocess (PII threshold override / parse retry); a checker decides."""
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return {"request_id": repo.request_release(conn, exception_id, body.comment)}


@router.post("/exceptions/{exception_id}/exclude")
def exclude_exception(exception_id: str, body: RequiredReason, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        repo.exclude_exception(conn, exception_id, body.reason)
        return repo.get_exception(conn, exception_id)


# ---------------------------------------------------------------------------- legal holds
class HoldCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str | None = None
    source_id: str | None = None
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/legal-holds", status_code=status.HTTP_201_CREATED)
def place_hold(body: HoldCreate, ident: Ident, st: St) -> Any:
    """Placing a hold only restricts (blocks purge): single actor, any MAKER/CHECKER of the BU."""
    if (body.doc_id is None) == (body.source_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "give exactly one of doc_id, source_id")
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        bu = repo.hold_bu(conn, body.doc_id, body.source_id)
        _require(sorted(set(roles(st, ident, "MAKER")) | set(roles(st, ident, "CHECKER"))), bu, "MAKER or CHECKER")
        return {"hold_id": repo.place_legal_hold(conn, body.doc_id, body.source_id, body.reason)}


@router.post("/legal-holds/{hold_id}/release", status_code=status.HTTP_201_CREATED)
def request_hold_release(hold_id: str, body: Comment, ident: Ident, st: St) -> Any:
    """Releasing a hold re-enables purge: four eyes (maker requests, checker decides)."""
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return {"request_id": repo.request_hold_release(conn, hold_id, body.comment)}


# ---------------------------------------------------------------------------- answer feedback (flags)
@router.get("/feedback")
def list_feedback(ident: Ident, st: St, status_filter: Literal["OPEN", "RESOLVED"] = "OPEN") -> Any:
    """Flags on answers that cited this BU's documents. Each BU sees only its own part of a flag (the
    versions of its documents); the question text is never shown (it is auditor-only)."""
    bus = sorted(set(roles(st, ident, "MAKER")) | set(roles(st, ident, "CHECKER")))
    if not bus:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not a MAKER or CHECKER for any BU")
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return conn.execute(
            """SELECT t.feedback_id, t.bu, t.status, t.resolved_by, t.resolved_at, t.resolution,
                      f.user_id, f.problem, f.reason, f.created_at,
                      (SELECT json_agg(json_build_object('version_id', v.version_id, 'doc_id', v.doc_id,
                              'version_no', v.version_no, 'title', d.title, 'is_current', v.is_current)
                              ORDER BY v.version_id)
                         FROM kb.document_version v JOIN kb.document d USING (doc_id)
                        WHERE v.version_id = ANY(t.version_ids)) AS versions
               FROM kb.feedback_target t JOIN kb.answer_feedback f USING (feedback_id)
               WHERE t.bu = ANY(%s) AND t.status = %s ORDER BY f.created_at DESC""",
            (bus, status_filter),
        ).fetchall()


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bu: str
    resolution: str = Field(min_length=1, max_length=2000)


@router.post("/feedback/{feedback_id}/resolve")
def resolve_feedback(feedback_id: str, body: Resolution, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        conn.execute("SELECT kb.resolve_feedback(%s, %s, %s)", (feedback_id, body.bu, body.resolution))
        return conn.execute(
            "SELECT * FROM kb.feedback_target WHERE feedback_id = %s AND bu = %s", (feedback_id, body.bu)
        ).fetchone()


# ---------------------------------------------------------------------------- ontology review queue
@router.get("/concept-reviews")
def concept_reviews(ident: Ident, st: St, status_filter: str = "PENDING") -> Any:
    bus = roles(st, ident, "CHECKER")
    if not bus:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not a CHECKER for any BU")
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        return conn.execute(
            "SELECT * FROM kb.concept_review WHERE bu = ANY(%s) AND status = %s ORDER BY confidence DESC, created_at",
            (bus, status_filter),
        ).fetchall()


@router.post("/concept-reviews/{review_id}/accept")
def accept_concept(review_id: str, body: Reason, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        conn.execute("SELECT kb.decide_concept(%s, true, %s)", (review_id, body.reason))
        return conn.execute("SELECT * FROM kb.concept_review WHERE review_id = %s", (review_id,)).fetchone()


@router.post("/concept-reviews/{review_id}/reject")
def reject_concept(review_id: str, body: RequiredReason, ident: Ident, st: St) -> Any:
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        conn.execute("SELECT kb.decide_concept(%s, false, %s)", (review_id, body.reason))
        return conn.execute("SELECT * FROM kb.concept_review WHERE review_id = %s", (review_id,)).fetchone()
