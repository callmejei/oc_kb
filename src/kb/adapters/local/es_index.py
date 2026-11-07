"""ElasticsearchChunkIndex: the chunk index behind an alias, with ACL pre-filtering.

Rule 4 (CLAUDE.md): the access filter is part of BOTH the BM25 query and the kNN clause, never
applied to results afterwards. The request bodies are built by pure functions so tests can inspect
them. Field names (``content``, ``embedding``, flat metadata) are compatible with Haystack's
Elasticsearch document store (Phase 3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from elasticsearch import Elasticsearch, helpers

from kb.ports.types import AccessFilter, Chunk, ScoredChunk

INDEX_VERSION = "v1"


def mapping(dims: int) -> dict[str, Any]:
    kw = {"type": "keyword"}
    return {
        "dynamic": "strict",
        "properties": {
            "chunk_id": kw,
            "doc_id": kw,
            "version_id": kw,
            "version_no": {"type": "integer"},
            "source_id": kw,
            "bu": kw,
            "origin": kw,
            "title": {"type": "text", "analyzer": "english"},
            "section_path": {"type": "text", "analyzer": "english", "fields": {"raw": kw}},
            "page_start": {"type": "integer"},
            "page_end": {"type": "integer"},
            "content": {"type": "text", "analyzer": "english"},
            "embedding": {
                "type": "dense_vector",
                "dims": dims,
                "index": True,
                "similarity": "cosine",
                "index_options": {"type": "hnsw"},  # no quantization: small corpus, exact scores
            },
            "acl_groups": kw,
            "is_current": {"type": "boolean"},
            "source_active": {"type": "boolean"},
            "concept_ids": kw,
            "content_sha": kw,
            "chunk_no": {"type": "integer"},
            "indexed_at": {"type": "date"},
            "doc_items": {"type": "object", "enabled": False},  # Docling refs for the inspector
        },
    }


def render_filter(flt: AccessFilter) -> list[dict[str, Any]]:
    """The mandatory pre-filter. No principals -> matches nothing (never 'no filter')."""
    if not flt.acl_principals:
        return [{"match_none": {}}]
    out: list[dict[str, Any]] = [{"terms": {"acl_groups": sorted(flt.acl_principals)}}]
    if flt.require_current:
        out.append({"term": {"is_current": True}})
    if flt.require_source_active:
        out.append({"term": {"source_active": True}})
    if flt.bus is not None:
        out.append({"terms": {"bu": sorted(flt.bus)}})
    return out


def bm25_body(query: str, flt: AccessFilter, k: int) -> dict[str, Any]:
    return {
        "size": k,
        "query": {
            "bool": {
                "must": {"multi_match": {"query": query, "fields": ["content", "title^2", "section_path"]}},
                "filter": render_filter(flt),
            }
        },
        "_source": {"excludes": ["embedding"]},
    }


def knn_body(vector: Sequence[float], flt: AccessFilter, k: int) -> dict[str, Any]:
    return {
        "size": k,
        "knn": {
            "field": "embedding",
            "query_vector": list(vector),
            "k": k,
            "num_candidates": max(100, 4 * k),
            "filter": render_filter(flt),
        },
        "_source": {"excludes": ["embedding"]},
    }


class ElasticsearchChunkIndex:
    STATUS = "implemented"
    PHASE = 2

    def __init__(
        self, url: str, index_alias: str, dims: int, user: str = "elastic", password: str = "", **_: Any
    ) -> None:
        self.alias = index_alias
        self.index = f"{index_alias}-{INDEX_VERSION}"
        self.dims = int(dims)
        self.es = Elasticsearch(url, basic_auth=(user, password), request_timeout=60)

    # ---------------------------------------------------------------- admin
    def ensure_index(self) -> None:
        if self.es.indices.exists_alias(name=self.alias):
            return
        self.es.indices.create(
            index=self.index,
            settings={"number_of_shards": 1, "number_of_replicas": 0},
            mappings=mapping(self.dims),
            aliases={self.alias: {}},
        )

    def drop(self) -> None:
        """Tests only: remove the index behind the alias."""
        self.es.indices.delete(index=self.index, ignore_unavailable=True)

    def refresh(self) -> None:
        self.es.indices.refresh(index=self.alias)

    # ---------------------------------------------------------------- writes
    def upsert(self, chunks: Sequence[Chunk]) -> None:
        now = datetime.now(UTC).isoformat()
        actions = [
            {
                "_op_type": "index",
                "_index": self.alias,
                "_id": c.chunk_id,
                "_source": {
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "version_id": c.version_id,
                    "bu": c.bu,
                    "content": c.text,
                    "embedding": list(c.embedding),
                    "acl_groups": list(c.acl_groups),
                    "is_current": c.is_current,
                    "source_active": c.source_active,
                    "page_start": c.page,
                    "section_path": c.section_path,
                    "concept_ids": list(c.concept_ids),
                    "indexed_at": now,
                    **dict(c.metadata),
                },
            }
            for c in chunks
        ]
        if actions:
            helpers.bulk(self.es, actions, refresh="wait_for")

    def _update(self, query: dict[str, Any], source: str, params: dict[str, Any]) -> int:
        r = self.es.update_by_query(
            index=self.alias,
            query=query,
            script={"source": source, "lang": "painless", "params": params},
            refresh=True,
            conflicts="proceed",
            slices="auto",
        )
        if r.get("failures"):
            raise RuntimeError(f"update_by_query failures: {r['failures'][:3]}")
        return int(r.get("updated", 0))

    def set_visibility(self, doc_id: str, *, current_version_id: str | None) -> None:
        """One request flips every chunk of the document: current version visible, others hidden."""
        self._update(
            {"term": {"doc_id": doc_id}},
            "ctx._source.is_current = params.v != null && ctx._source.version_id == params.v",
            {"v": current_version_id},
        )

    def restamp_acl(self, doc_id: str, acl_groups: Sequence[str]) -> None:
        if not acl_groups:
            raise ValueError("refusing to stamp an empty ACL")
        self._update({"term": {"doc_id": doc_id}}, "ctx._source.acl_groups = params.g", {"g": list(acl_groups)})

    def set_source_active(self, source_id: str, active: bool) -> None:
        self._update({"term": {"source_id": source_id}}, "ctx._source.source_active = params.a", {"a": active})

    def set_concepts(self, concepts: dict[str, list[str]]) -> int:
        """Partial update of concept_ids per chunk (no re-embedding). Returns chunks updated."""
        actions = [
            {"_op_type": "update", "_index": self.alias, "_id": cid, "doc": {"concept_ids": sorted(set(c))}}
            for cid, c in concepts.items()
        ]
        if not actions:
            return 0
        ok, _ = helpers.bulk(self.es, actions, refresh="wait_for", raise_on_error=False)
        return int(ok)

    def concept_search(
        self, terms: Sequence[str], concept_ids: Sequence[str], flt: AccessFilter, k: int
    ) -> list[ScoredChunk]:
        """Graph-expansion retriever: expansion terms over content + concept_ids, inside the same mandatory
        pre-filter as BM25 and kNN."""
        should: list[dict[str, Any]] = []
        if concept_ids:
            should.append({"terms": {"concept_ids": list(concept_ids), "boost": 2.0}})
        should += [{"match_phrase": {"content": {"query": t, "slop": 2}}} for t in terms]
        if not should:
            return []
        body = {
            "size": k,
            "query": {"bool": {"should": should, "minimum_should_match": 1, "filter": render_filter(flt)}},
            "_source": {"excludes": ["embedding"]},
        }
        return self._hits(body, "concept")

    def delete_version(self, version_id: str) -> None:
        self.es.delete_by_query(index=self.alias, query={"term": {"version_id": version_id}}, refresh=True)

    # ---------------------------------------------------------------- reads
    def _hits(self, body: dict[str, Any], retriever: str) -> list[ScoredChunk]:
        r = self.es.search(index=self.alias, **body)
        out = []
        for h in r["hits"]["hits"]:
            s = h["_source"]
            meta = {
                k: v
                for k, v in s.items()
                if k
                not in {
                    "chunk_id",
                    "doc_id",
                    "version_id",
                    "content",
                    "acl_groups",
                    "bu",
                    "is_current",
                    "source_active",
                    "page_start",
                    "section_path",
                    "concept_ids",
                }
            }
            out.append(
                ScoredChunk(
                    chunk=Chunk(
                        chunk_id=s["chunk_id"],
                        doc_id=s["doc_id"],
                        version_id=s["version_id"],
                        text=s["content"],
                        embedding=(),
                        acl_groups=s["acl_groups"],
                        bu=s["bu"],
                        is_current=s["is_current"],
                        source_active=s["source_active"],
                        page=s.get("page_start"),
                        section_path=s.get("section_path"),
                        concept_ids=s.get("concept_ids") or (),
                        metadata=meta,
                    ),
                    score=float(h["_score"] or 0.0),
                    retriever=retriever,
                )
            )
        return out

    def bm25(self, query: str, flt: AccessFilter, k: int) -> list[ScoredChunk]:
        return self._hits(bm25_body(query, flt, k), "bm25")

    def knn(self, vector: Sequence[float], flt: AccessFilter, k: int) -> list[ScoredChunk]:
        return self._hits(knn_body(vector, flt, k), "knn")

    # ---------------------------------------------------------------- document inspector (ACL-filtered)
    def visible_versions(self, doc_id: str, flt: AccessFilter) -> set[str]:
        """Versions of a document with at least one chunk the filter lets through."""
        r = self.es.search(
            index=self.alias,
            size=0,
            query={"bool": {"filter": [{"term": {"doc_id": doc_id}}, *render_filter(flt)]}},
            aggs={"v": {"terms": {"field": "version_id", "size": 500}}},
        )
        return {b["key"] for b in r["aggregations"]["v"]["buckets"]}

    def version_chunks(self, version_id: str, flt: AccessFilter, size: int = 2000) -> list[ScoredChunk]:
        body = {
            "size": size,
            "query": {"bool": {"filter": [{"term": {"version_id": version_id}}, *render_filter(flt)]}},
            "sort": [{"chunk_no": "asc"}],
            "_source": {"excludes": ["embedding"]},
        }
        return self._hits(body, "inspect")

    # ---------------------------------------------------------------- inspection (pipeline/tests)
    def count(self, query: dict[str, Any] | None = None) -> int:
        return int(self.es.count(index=self.alias, query=query or {"match_all": {}})["count"])

    def docs(self, query: dict[str, Any], size: int = 1000) -> list[dict[str, Any]]:
        r = self.es.search(index=self.alias, query=query, size=size, source_excludes=["embedding"])
        return [h["_source"] for h in r["hits"]["hits"]]
