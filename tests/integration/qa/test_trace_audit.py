"""Phase 7: every question's Phoenix trace and its QUERY audit row point at each other, and spans hold no
question or answer text (D-056). Real retriever and LLM; spans captured in memory."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kb.adapters import build
from kb.adapters.local.tracer import PhoenixTracer
from kb.agent.service import AskService

QUESTION = "What is the maximum loan-to-value ratio for a residential property loan?"


@pytest.fixture(scope="module")
def traced(qa_settings, retriever, registry):
    mem = InMemorySpanExporter()
    svc = AskService(
        retriever,
        build(qa_settings, "llm"),
        registry,
        qa_settings.retrieval,
        tracer=PhoenixTracer("http://127.0.0.1:6006", exporter=mem),
    )
    return svc, mem


def test_trace_and_audit_row_link_both_ways(traced, idp, registry):
    svc, mem = traced
    mem.clear()
    r = svc.ask(idp.resolve("ret.user1"), QUESTION)
    spans = mem.get_finished_spans()
    root = next(s for s in spans if s.name == "kb.ask")
    trace_id = format(root.context.trace_id, "032x")

    # trace -> audit
    assert root.attributes["kb.audit_id"] == r.audit_id and root.attributes["kb.query_id"] == r.query_id
    # audit -> trace
    with registry.transaction("ret.user1") as tx:
        row = tx.execute("SELECT details FROM kb.audit_log WHERE audit_id = %s", (r.audit_id,)).fetchone()
    assert row["details"]["trace_id"] == trace_id

    names = {s.name for s in spans if s.context.trace_id == root.context.trace_id}
    assert {"kb.ask", "kb.route", "kb.retrieve", "kb.answer"} <= names


def test_spans_carry_no_question_or_answer_text(traced, idp):
    svc, mem = traced
    mem.clear()
    r = svc.ask(idp.resolve("ret.user1"), QUESTION)
    blob = " ".join(str(v) for s in mem.get_finished_spans() for v in s.attributes.values()).lower()
    assert "loan-to-value" not in blob and "residential" not in blob
    for sentence in r.answer.text.split(". "):
        if len(sentence) > 25:
            assert sentence.lower()[:25] not in blob
