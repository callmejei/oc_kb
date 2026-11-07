"""DTOs shared by the ports. Plain frozen dataclasses: no framework types cross a port."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ChangeKind(StrEnum):
    UPSERT = "upsert"  # new or changed file (cTag differs)
    DELETED = "deleted"  # Graph "deleted" facet
    OUT_OF_SCOPE = "out_of_scope"  # moved outside the onboarded folder


@dataclass(frozen=True)
class SourceItem:
    """One item from a document source (SharePoint driveItem or upload)."""

    external_item_id: str  # stable across renames and moves
    name: str
    path: str  # path relative to the source root
    size_bytes: int
    ctag: str  # content tag: changes only when content changes
    etag: str
    modified_at: datetime
    change: ChangeKind = ChangeKind.UPSERT


@dataclass(frozen=True)
class DeltaPage:
    items: Sequence[SourceItem]
    next_token: str | None  # opaque; commit only after the whole run succeeds
    resync_required: bool = False  # token expired (Graph 410): caller must do a full resync


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    scanned: bool  # False when the adapter only did type checks ("not scanned")
    detected_type: str | None
    reason: str | None = None


@dataclass(frozen=True)
class StoredObject:
    uri: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class Identity:
    user_id: str
    display_name: str
    bu: str
    groups: frozenset[str]

    @property
    def acl_principals(self) -> frozenset[str]:
        """Values matched against chunk ``acl_groups``: AD groups plus the private principal."""
        return self.groups | {f"user:{self.user_id}"}


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    version_id: str
    text: str
    embedding: Sequence[float]
    acl_groups: Sequence[str]
    bu: str
    is_current: bool
    source_active: bool
    page: int | None = None
    section_path: str | None = None
    concept_ids: Sequence[str] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccessFilter:
    """Mandatory pre-filter. Built only by the gateway from a resolved Identity."""

    acl_principals: frozenset[str]
    require_current: bool = True
    require_source_active: bool = True
    bus: frozenset[str] | None = None


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float
    retriever: str  # "bm25" | "knn" | "rrf" | "rerank"


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class SqlResult:
    columns: Sequence[str]
    rows: Sequence[Sequence[Any]]
    executed_sql: str  # the SQL after policy rewrite, for citation and audit
    tables: Sequence[str]
