"""Tracer adapters: nesting, attribute hygiene, endpoint guards (no network: in-memory exporter)."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kb.adapters.local.tracer import NullTracer, OtelTracer, PhoenixTracer
from kb.ports import Tracer


def test_nested_spans_share_one_trace_and_ids_are_hex():
    mem = InMemorySpanExporter()
    t = PhoenixTracer("http://127.0.0.1:6006", exporter=mem)
    assert isinstance(t, Tracer)
    with t.span("kb.ask", **{"kb.user_id": "u1"}) as root:
        with t.span("kb.retrieve") as child:
            child.set(**{"kb.retrieved": 3, "kb.none": None, "kb.list": ("a", 1), "kb.map": {"x": 1}})
        root.set(**{"kb.audit_id": 42})
    spans = {s.name: s for s in mem.get_finished_spans()}
    assert set(spans) == {"kb.ask", "kb.retrieve"}
    assert len(root.trace_id) == 32 and int(root.trace_id, 16)
    assert child.trace_id == root.trace_id
    assert spans["kb.retrieve"].parent.span_id == spans["kb.ask"].context.span_id
    a = spans["kb.retrieve"].attributes
    assert (
        a["kb.retrieved"] == 3 and "kb.none" not in a and list(a["kb.list"]) == ["a", "1"] and a["kb.map"] == '{"x": 1}'
    )
    assert spans["kb.ask"].attributes["kb.audit_id"] == 42
    assert spans["kb.ask"].resource.attributes["openinference.project.name"] == "kb"


def test_local_tracer_refuses_non_loopback_endpoint():
    with pytest.raises(ValueError, match="not a loopback"):
        PhoenixTracer("https://phoenix.example.com")
    PhoenixTracer("http://localhost:6006")  # fine


def test_cml_tracer_requires_https():
    with pytest.raises(ValueError, match="https"):
        OtelTracer("http://collector.internal:4318")
    OtelTracer("https://collector.internal", exporter=InMemorySpanExporter())


def test_null_tracer_is_a_tracer_with_empty_trace_id():
    t = NullTracer()
    assert isinstance(t, Tracer)
    with t.span("x", a=1) as s:
        s.set(b=2)
        assert s.trace_id == ""
