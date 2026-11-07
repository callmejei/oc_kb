"""AskService: filter -> retrieve -> answer -> audit. The gateway's only entry to documents."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from psycopg.types.json import Jsonb

from kb.adapters.local.registry import PostgresRegistry
from kb.agent.answer import Answer, answer
from kb.gateway.acl import build_access_filter, describe
from kb.ports import LLM
from kb.ports.types import Identity
from kb.retrieval.pipeline import HybridRetriever


@dataclass
class AskResult:
    answer: Answer
    query_id: str
    audit_id: int
    latency_ms: int
    retrieved_ids: list[str]

    def as_response(self) -> dict[str, Any]:
        return {
            "answer": self.answer.text,
            "citations": [asdict(c) for c in self.answer.citations],
            "grounded": self.answer.grounded,
            "query_id": self.query_id,
            "audit_id": self.audit_id,
            "latency_ms": self.latency_ms,
        }


class AskService:
    def __init__(self, retriever: HybridRetriever, llm: LLM, registry: PostgresRegistry, cfg: dict[str, Any]) -> None:
        self.retriever, self.llm, self.registry, self.cfg = retriever, llm, registry, cfg

    def ask(self, identity: Identity, question: str) -> AskResult:
        t0 = time.monotonic()
        flt = build_access_filter(identity)
        ret = self.retriever.retrieve(question, flt)
        ans = answer(question, ret.context, self.llm, int(self.cfg.get("max_context_chars", 9000)))
        latency = int((time.monotonic() - t0) * 1000)
        query_id = str(uuid.uuid4())
        details = {
            "groups": sorted(identity.groups),
            "filter": describe(flt),
            "retrieved_chunk_ids": ret.retrieved_ids,
            "context_chunk_ids": ans.context_ids,
            "cited_chunk_ids": [c.chunk_id for c in ans.citations],
            "cited_doc_versions": sorted({c.version_id for c in ans.citations if c.version_id}),
            "invalid_citations": ans.invalid_citations,
            "context_redactions": ans.redactions,
            "blocked_reason": ans.blocked_reason,
            "llm_called": ans.llm_called,
            "grounded": ans.grounded,
            "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "answer_sha256": ans.sha256,
            "latency_ms": latency,
        }
        with self.registry.transaction(identity.user_id) as c:
            c.execute(
                "INSERT INTO kb.query_log (query_id, user_id, question) VALUES (%s, kb.current_actor(), %s)",
                (query_id, question),
            )
            row = c.execute(
                """INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, details)
                   VALUES (kb.current_actor(), 'QUERY', 'query', %s, %s) RETURNING audit_id""",
                (query_id, Jsonb(details)),
            ).fetchone()
        assert row is not None
        return AskResult(ans, query_id, int(row["audit_id"]), latency, ret.retrieved_ids)
