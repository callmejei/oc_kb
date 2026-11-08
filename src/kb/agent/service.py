"""AskService: route -> (documents and/or data) -> answer -> audit. The gateway's only entry to content.

Documents go through the mandatory ACL pre-filter; data goes through text-to-SQL and the PolicyEngine
(grants, row filters, masks). Every question writes one QUERY audit row.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from kb.adapters.local.registry import PostgresRegistry
from kb.adapters.local.tracer import NullTracer
from kb.agent import router
from kb.agent.answer import Answer, DataSource, answer
from kb.gateway.acl import build_access_filter, describe
from kb.ports import LLM, Tracer
from kb.ports.types import Identity, ScoredChunk
from kb.retrieval.pipeline import HybridRetriever
from kb.sql.text_to_sql import SqlAttempt, TextToSql

NO_DATA_ACCESS_NOTE = "You don't have access to structured data for this question; the answer uses documents only."
DATA_FAILED_NOTE = "This question could not be answered from the data you have access to."


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class AskResult:
    answer: Answer
    query_id: str
    audit_id: int
    latency_ms: int
    retrieved_ids: list[str]
    route: str = "documents"
    notes: list[str] = field(default_factory=list)

    def as_response(self) -> dict[str, Any]:
        return {
            "answer": self.answer.text,
            "citations": [asdict(c) for c in self.answer.citations],
            "grounded": self.answer.grounded,
            "route": self.route,
            "notes": self.notes,
            "query_id": self.query_id,
            "audit_id": self.audit_id,
            "latency_ms": self.latency_ms,
        }


@dataclass
class Computed:
    answer: Answer
    route: str
    route_method: str
    data_question: str
    notes: list[str]
    sql: SqlAttempt | None
    retrieved_ids: list[str]
    filter: dict[str, Any]
    latency_ms: int
    expansion: dict[str, Any] = field(default_factory=dict)


class AskService:
    def __init__(
        self,
        retriever: HybridRetriever,
        llm: LLM,
        registry: PostgresRegistry,
        cfg: dict[str, Any],
        text_to_sql: TextToSql | None = None,
        scope_fn: Any = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.retriever, self.llm, self.registry, self.cfg, self.t2s = retriever, llm, registry, cfg, text_to_sql
        self.tracer: Tracer = tracer or NullTracer()
        self.scope_fn = scope_fn  # identity -> BUs whose graphs may be read for expansion (None: no expansion)

    def ask(self, identity: Identity, question: str, spaces: list[str] | None = None) -> AskResult:
        with self.tracer.span(
            "kb.ask",
            **{"kb.user_id": identity.user_id, "kb.spaces": spaces or [], "kb.question_sha256": _sha(question)},
        ) as sp:
            c = self.compute(identity, question, spaces)
            res = self._audit(identity, question, c, trace_id=sp.trace_id or None)
            sp.set(
                **{
                    "kb.audit_id": res.audit_id,
                    "kb.query_id": res.query_id,
                    "kb.route": c.route,
                    "kb.grounded": c.answer.grounded,
                    "kb.blocked_reason": c.answer.blocked_reason,
                    "kb.retrieved": len(c.retrieved_ids),
                    "kb.cited": len(c.answer.citations),
                    "kb.latency_ms": c.latency_ms,
                }
            )
            return res

    def compute(self, identity: Identity, question: str, spaces: list[str] | None = None) -> Computed:
        """Everything except the audit write (used by ask() and by offline evaluation).
        spaces: BUs the user picked; narrows documents and graph expansion (never widens)."""
        t0 = time.monotonic()
        flt = build_access_filter(identity, spaces)
        tables = self.t2s.engine.policy.granted_tables(identity) if self.t2s else []
        with self.tracer.span("kb.route", **{"kb.granted_tables": len(tables)}) as sp:
            route, route_method, data_question = router.route(question, self.llm, tables)
            sp.set(**{"kb.route": route, "kb.route_method": route_method})
        notes: list[str] = []
        if not tables and router.heuristic(question) in ("data", "both"):
            notes.append(NO_DATA_ACCESS_NOTE)

        sql: SqlAttempt | None = None
        data: list[DataSource] = []
        if route in ("data", "both") and self.t2s:
            with self.tracer.span("kb.sql") as sp:
                sql = self.t2s.run(data_question, identity)
                sp.set(
                    **{
                        "kb.ok": bool(sql.data),
                        "kb.attempts": len(sql.attempts),
                        "kb.tables": sql.data.tables if sql.data else [],
                    }
                )
            if sql.data:
                data.append(sql.data)
            else:
                notes.append(DATA_FAILED_NOTE)
        context: list[ScoredChunk] = []
        retrieved_ids: list[str] = []
        expansion: dict[str, Any] = {}
        if route in ("documents", "both") or not data:  # data failed -> fall back to documents
            scope = self.scope_fn(identity) if self.scope_fn else None
            if scope is not None and spaces:
                scope = [b for b in scope if b in spaces]
            with self.tracer.span("kb.retrieve", **{"kb.graph_scope": scope or []}) as sp:
                ret = self.retriever.retrieve(question, flt, scope=scope)
                sp.set(
                    **{
                        "kb.retrieved": len(ret.retrieved_ids),
                        "kb.context": len(ret.context),
                        "kb.expansion_concepts": len(ret.expansion.concepts) if ret.expansion else 0,
                    }
                )
            context, retrieved_ids = ret.context, ret.retrieved_ids
            if ret.expansion is not None:
                expansion = {
                    "concepts": ret.expansion.concepts,
                    "related": ret.expansion.related,
                    "graphs": ret.expansion.graphs,
                    "terms": ret.expansion.terms,
                }
        with self.tracer.span("kb.answer", **{"kb.context": len(context), "kb.data_sources": len(data)}) as sp:
            ans = answer(question, context, self.llm, int(self.cfg.get("max_context_chars", 9000)), data=data)
            sp.set(
                **{
                    "kb.llm_called": ans.llm_called,
                    "kb.grounded": ans.grounded,
                    "kb.blocked_reason": ans.blocked_reason,
                    "kb.invalid_citations": ans.invalid_citations,
                }
            )
        latency = int((time.monotonic() - t0) * 1000)
        return Computed(
            ans, route, route_method, data_question, notes, sql, retrieved_ids, describe(flt), latency, expansion
        )

    def _audit(self, identity: Identity, question: str, c: Computed, trace_id: str | None = None) -> AskResult:
        ans, route, route_method, data_question, notes, sql, retrieved_ids, latency = (
            c.answer,
            c.route,
            c.route_method,
            c.data_question,
            c.notes,
            c.sql,
            c.retrieved_ids,
            c.latency_ms,
        )
        query_id = str(uuid.uuid4())
        details = {
            "groups": sorted(identity.groups),
            "filter": c.filter,
            "route": route,
            "route_method": route_method,
            "data_question_sha256": hashlib.sha256(data_question.encode()).hexdigest(),
            "retrieved_chunk_ids": retrieved_ids,
            "graph_expansion": c.expansion,
            "context_chunk_ids": ans.context_ids,
            "cited_chunk_ids": [c.chunk_id for c in ans.citations if c.chunk_id],
            "cited_doc_versions": sorted({c.version_id for c in ans.citations if c.version_id}),
            "sql": {
                "attempts": sql.attempts if sql else [],
                "executed_sql": sql.data.executed_sql if sql and sql.data else None,
                "tables": sql.data.tables if sql and sql.data else [],
                "row_count": len(sql.data.rows) if sql and sql.data else None,
                "policy": sql.decision if sql else {},
            },
            "invalid_citations": ans.invalid_citations,
            "remapped_citations": ans.remapped_citations,
            "context_redactions": ans.redactions,
            "blocked_reason": ans.blocked_reason,
            "llm_called": ans.llm_called,
            "grounded": ans.grounded,
            "notes": notes,
            "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "answer_sha256": ans.sha256,
            "latency_ms": latency,
            "trace_id": trace_id,  # Phoenix trace; its kb.ask span carries this audit_id
        }
        with self.registry.transaction(identity.user_id) as tx:
            tx.execute(
                "INSERT INTO kb.query_log (query_id, user_id, question) VALUES (%s, kb.current_actor(), %s)",
                (query_id, question),
            )
            row = tx.execute(
                """INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, details)
                   VALUES (kb.current_actor(), 'QUERY', 'query', %s, %s) RETURNING audit_id""",
                (query_id, Jsonb(details)),
            ).fetchone()
        assert row is not None
        return AskResult(ans, query_id, int(row["audit_id"]), latency, retrieved_ids, route, notes)
