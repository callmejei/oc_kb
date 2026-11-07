"""Ports: one Protocol per external system (CLAUDE.md section 4).

Local and CML deployments differ only in the adapters bound to these Protocols.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, BinaryIO, Protocol, runtime_checkable

from kb.ports.types import (
    AccessFilter,
    ChatMessage,
    Chunk,
    DeltaPage,
    Identity,
    ScanResult,
    ScoredChunk,
    SourceItem,
    SqlResult,
    StoredObject,
)


@runtime_checkable
class DocumentSource(Protocol):
    """One onboarded library with Graph delta semantics.

    ``delta(None)`` returns the current state; later tokens return changes since. Only the last page
    carries ``next_token``; a caller commits it only after the whole run succeeded. A page with
    ``resync_required`` means the token expired: the caller must reconcile with ``list_all``.
    """

    def delta(self, token: str | None) -> Iterator[DeltaPage]: ...
    def list_all(self) -> Iterator[SourceItem]: ...
    def open(self, item: SourceItem) -> AbstractContextManager[BinaryIO]: ...


@runtime_checkable
class DocumentSourceFactory(Protocol):
    """Binds the configured connector to one source (site, drive, folder from the approved revision)."""

    def bind(self, site_id: str, drive_id: str, folder_path: str) -> DocumentSource: ...


@runtime_checkable
class MalwareScanner(Protocol):
    def scan(self, stream: BinaryIO, declared_name: str) -> ScanResult: ...


@runtime_checkable
class RawStore(Protocol):
    def put_stream(self, key: str, stream: BinaryIO) -> StoredObject: ...
    def open(self, uri: str) -> AbstractContextManager[BinaryIO]: ...
    def exists(self, uri: str) -> bool: ...
    def delete(self, uri: str) -> None: ...
    def uri_for(self, key: str) -> str: ...


@runtime_checkable
class Registry(Protocol):
    """Registry access. ``transaction`` sets kb.actor_id for everything inside it."""

    def transaction(self, actor_id: str) -> AbstractContextManager[Any]: ...
    def ping(self) -> bool: ...


@runtime_checkable
class ChunkIndex(Protocol):
    def ensure_index(self) -> None: ...
    def upsert(self, chunks: Sequence[Chunk]) -> None: ...
    def set_visibility(self, doc_id: str, *, current_version_id: str | None) -> None: ...
    def restamp_acl(self, doc_id: str, acl_groups: Sequence[str]) -> None: ...
    def set_source_active(self, source_id: str, active: bool) -> None: ...
    def delete_version(self, version_id: str) -> None: ...
    def set_concepts(self, concepts: Mapping[str, list[str]]) -> int: ...
    def concept_search(
        self, terms: Sequence[str], concept_ids: Sequence[str], flt: AccessFilter, k: int
    ) -> Sequence[ScoredChunk]: ...
    def bm25(self, query: str, flt: AccessFilter, k: int) -> Sequence[ScoredChunk]: ...
    def knn(self, vector: Sequence[float], flt: AccessFilter, k: int) -> Sequence[ScoredChunk]: ...
    def visible_versions(self, doc_id: str, flt: AccessFilter) -> set[str]: ...
    def version_chunks(self, version_id: str, flt: AccessFilter, size: int = 2000) -> Sequence[ScoredChunk]: ...


@runtime_checkable
class Embedder(Protocol):
    @property
    def dims(self) -> int: ...
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, candidates: Sequence[ScoredChunk], top_k: int) -> list[ScoredChunk]: ...


@runtime_checkable
class LLM(Protocol):
    def chat(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> str: ...


@runtime_checkable
class SqlEngine(Protocol):
    """Executes validated SELECTs as the end user (policy rewrite locally, impersonation on CML)."""

    def permitted_schema(self, identity: Identity) -> Mapping[str, Sequence[str]]: ...
    def execute(self, sql: str, identity: Identity, *, row_limit: int) -> SqlResult: ...


@runtime_checkable
class IdentityProvider(Protocol):
    def resolve(self, user_id: str) -> Identity: ...
    def all_users(self) -> Sequence[Identity]: ...


@runtime_checkable
class GraphStore(Protocol):
    def replace_graph(self, graph_iri: str, ntriples: Iterator[str]) -> None: ...
    def query(self, sparql: str, graph_iris: Sequence[str]) -> list[dict[str, Any]]: ...


@runtime_checkable
class Tracer(Protocol):
    def span(self, name: str, **attributes: Any) -> AbstractContextManager[str]: ...


__all__ = [
    "LLM",
    "ChunkIndex",
    "DocumentSource",
    "DocumentSourceFactory",
    "Embedder",
    "GraphStore",
    "IdentityProvider",
    "MalwareScanner",
    "RawStore",
    "Registry",
    "Reranker",
    "SqlEngine",
    "Tracer",
]

PORT_PROTOCOLS: dict[str, type] = {
    "document_source": DocumentSourceFactory,
    "upload_source": DocumentSourceFactory,
    "malware_scanner": MalwareScanner,
    "raw_store": RawStore,
    "registry": Registry,
    "chunk_index": ChunkIndex,
    "embedder": Embedder,
    "reranker": Reranker,
    "llm": LLM,
    "sql_engine": SqlEngine,
    "identity_provider": IdentityProvider,
    "graph_store": GraphStore,
    "tracer": Tracer,
}
