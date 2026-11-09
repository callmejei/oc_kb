"""Hybrid retrieval as a Haystack 2.x Pipeline:

    query ─┬─ AclBm25Retriever ──┐
           └─ AclKnnRetriever ───┴─ DocumentJoiner(reciprocal_rank_fusion) ─ ChunkReranker ─► context

Both retrievers go through the ChunkIndex port with the caller's AccessFilter, so the ACL pre-filter is
inside both Elasticsearch queries (rule 4). There is deliberately no component that searches without a
filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from haystack import Document, Pipeline, component
from haystack.components.joiners import DocumentJoiner

from kb.ports import ChunkIndex, Embedder, Reranker
from kb.ports.types import AccessFilter, Chunk, ScoredChunk


def to_document(sc: ScoredChunk) -> Document:
    c = sc.chunk
    meta = {
        "doc_id": c.doc_id,
        "version_id": c.version_id,
        "bu": c.bu,
        "acl_groups": list(c.acl_groups),
        "is_current": c.is_current,
        "source_active": c.source_active,
        "page": c.page,
        "section_path": c.section_path,
        "concept_ids": list(c.concept_ids),
        **{k: v for k, v in dict(c.metadata).items() if k != "doc_items"},
    }
    return Document(id=c.chunk_id, content=c.text, meta=meta, score=sc.score)


def to_scored(d: Document) -> ScoredChunk:
    m = dict(d.meta)
    chunk = Chunk(
        chunk_id=d.id,
        doc_id=m.pop("doc_id"),
        version_id=m.pop("version_id"),
        text=d.content or "",
        embedding=(),
        acl_groups=m.pop("acl_groups"),
        bu=m.pop("bu"),
        is_current=m.pop("is_current"),
        source_active=m.pop("source_active"),
        page=m.pop("page", None),
        section_path=m.pop("section_path", None),
        concept_ids=m.pop("concept_ids", ()),
        metadata=m,
    )
    return ScoredChunk(chunk=chunk, score=float(d.score or 0.0), retriever="pipeline")


@component
class AclBm25Retriever:
    def __init__(self, index: ChunkIndex, top_k: int) -> None:
        self.index, self.top_k = index, top_k

    @component.output_types(documents=list[Document])
    def run(self, query: str, access_filter: AccessFilter) -> dict[str, Any]:
        return {"documents": [to_document(h) for h in self.index.bm25(query, access_filter, self.top_k)]}


@component
class AclKnnRetriever:
    def __init__(self, index: ChunkIndex, embedder: Embedder, top_k: int) -> None:
        self.index, self.embedder, self.top_k = index, embedder, top_k

    @component.output_types(documents=list[Document])
    def run(self, query: str, access_filter: AccessFilter) -> dict[str, Any]:
        vec = self.embedder.embed_query(query)
        return {"documents": [to_document(h) for h in self.index.knn(vec, access_filter, self.top_k)]}


@component
class ChunkReranker:
    def __init__(self, reranker: Reranker, top_k: int, min_score: float) -> None:
        self.reranker, self.top_k, self.min_score = reranker, top_k, min_score

    @component.output_types(documents=list[Document])
    def run(self, query: str, documents: list[Document]) -> dict[str, Any]:
        ranked = self.reranker.rerank(query, [to_scored(d) for d in documents], self.top_k)
        kept = [r for r in ranked if r.score >= self.min_score]
        return {"documents": [to_document(r) for r in kept]}


@component
class AclConceptRetriever:
    """Graph-expansion branch: expansion terms + concept ids, through the same mandatory pre-filter."""

    def __init__(self, index: ChunkIndex, top_k: int) -> None:
        self.index, self.top_k = index, top_k

    @component.output_types(documents=list[Document])
    def run(self, terms: list[str], concept_ids: list[str], access_filter: AccessFilter) -> dict[str, Any]:
        hits = self.index.concept_search(terms, concept_ids, access_filter, self.top_k)
        return {"documents": [to_document(h) for h in hits]}


@dataclass
class RetrievalResult:
    context: list[ScoredChunk]  # what the LLM may see, best first
    retrieved: list[ScoredChunk]  # everything any retriever returned (superset of context)
    expansion: Any = None  # kb.retrieval.expansion.Expansion when graph expansion ran

    @property
    def retrieved_ids(self) -> list[str]:
        return sorted({r.chunk.chunk_id for r in self.retrieved})


class HybridRetriever:
    def candidates(
        self, query: str, access_filter: AccessFilter, k: int | None = None, scope: list[str] | None = None
    ) -> list[ScoredChunk]:
        """Everything the pre-filtered retrievers return (no fusion / rerank), incl. the expansion branch.
        The LLM context is always a subset of this, so leak checks over it are strictly stronger."""
        k = k or max(self.bm25.top_k, self.knn.top_k)
        hits = [
            *self.index.bm25(query, access_filter, k),
            *self.index.knn(self.embedder.embed_query(query), access_filter, k),
        ]
        if self.expander is not None and scope is not None:
            exp = self.expander.expand(query, scope)
            hits += self.index.concept_search(exp.terms, exp.concept_ids, access_filter, k)
        return list({h.chunk.chunk_id: h for h in hits}.values())

    def __init__(
        self, index: ChunkIndex, embedder: Embedder, reranker: Reranker, cfg: dict[str, Any], expander: Any = None
    ) -> None:
        self.index, self.embedder, self.expander = index, embedder, expander
        p = Pipeline()
        self.bm25 = AclBm25Retriever(index, int(cfg.get("bm25_k", 20)))
        self.knn = AclKnnRetriever(index, embedder, int(cfg.get("knn_k", 20)))
        p.add_component("bm25", self.bm25)
        p.add_component("knn", self.knn)
        p.add_component("concept", AclConceptRetriever(index, int(cfg.get("bm25_k", 20))))
        p.add_component("fuse", DocumentJoiner(join_mode="reciprocal_rank_fusion", top_k=int(cfg.get("fused_k", 20))))
        p.add_component(
            "rerank", ChunkReranker(reranker, int(cfg.get("rerank_k", 6)), float(cfg.get("min_rerank_score", -4.0)))
        )
        p.connect("bm25.documents", "fuse.documents")
        p.connect("knn.documents", "fuse.documents")
        p.connect("concept.documents", "fuse.documents")
        p.connect("fuse.documents", "rerank.documents")
        self.pipeline = p

    def retrieve(self, query: str, access_filter: AccessFilter, scope: list[str] | None = None) -> RetrievalResult:
        """scope: BUs whose graphs the caller may read (None = no graph expansion)."""
        exp = self.expander.expand(query, scope) if (self.expander is not None and scope is not None) else None
        out = self.pipeline.run(
            {
                "bm25": {"query": query, "access_filter": access_filter},
                "knn": {"query": query, "access_filter": access_filter},
                "concept": {
                    "terms": exp.terms if exp else [],
                    "concept_ids": exp.concept_ids if exp else [],
                    "access_filter": access_filter,
                },
                "rerank": {"query": exp.rerank_query(query) if exp else query},
            },
            include_outputs_from={"bm25", "knn", "concept"},
        )
        seen: dict[str, ScoredChunk] = {}
        for k in ("bm25", "knn", "concept"):
            for d in out[k]["documents"]:
                seen.setdefault(d.id, to_scored(d))
        return RetrievalResult(
            context=[to_scored(d) for d in out["rerank"]["documents"]], retrieved=list(seen.values()), expansion=exp
        )
