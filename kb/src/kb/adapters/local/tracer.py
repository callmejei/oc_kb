"""Tracer adapters: OpenTelemetry spans exported over OTLP/HTTP to Arize Phoenix (local), or nothing.

Spans carry ids, counts and hashes, never question or answer text (D-056): the registry's query log is the
one access-controlled copy of what was asked. The ``kb.ask`` root span carries ``kb.audit_id`` /
``kb.query_id`` and the audit row carries the ``trace_id``, so each side finds the other.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode, format_trace_id

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _attr_value(v: Any) -> Any:
    if isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        return [str(x) for x in v]
    if isinstance(v, Mapping):
        return json.dumps(v, default=str, sort_keys=True)[:4000]
    return str(v)


def _clean(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _attr_value(v) for k, v in attributes.items() if v is not None}


class OtelSpan:
    def __init__(self, span: Span) -> None:
        self._span = span
        ctx = span.get_span_context()
        self.trace_id = format_trace_id(ctx.trace_id) if ctx.is_valid else ""

    def set(self, **attributes: Any) -> None:
        self._span.set_attributes(_clean(attributes))

    def error(self, message: str) -> None:
        self._span.set_status(Status(StatusCode.ERROR, message))


class _OtlpTracer:
    """OTLP/HTTP span export from a private provider (not the global one), so tests and the app stay apart."""

    STATUS = "implemented"

    def __init__(self, endpoint: str, project: str, service_name: str, exporter: SpanExporter | None) -> None:
        self.endpoint, self.project = endpoint.rstrip("/"), project
        resource = Resource.create({"service.name": service_name, "openinference.project.name": project})
        self._provider = TracerProvider(resource=resource)
        if exporter is None:
            otlp = OTLPSpanExporter(endpoint=f"{self.endpoint}/v1/traces", timeout=5)
            self._provider.add_span_processor(BatchSpanProcessor(otlp))
        else:  # tests inject an in-memory exporter
            self._provider.add_span_processor(SimpleSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("kb")

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[OtelSpan]:
        with self._tracer.start_as_current_span(name, attributes=_clean(attributes)) as s:
            yield OtelSpan(s)

    def flush(self, timeout_ms: int = 5000) -> bool:
        return self._provider.force_flush(timeout_ms)


class PhoenixTracer(_OtlpTracer):
    """Local: Phoenix's ``/v1/traces``. Refuses a non-loopback endpoint so traces never leave the laptop."""

    def __init__(
        self, endpoint: str, project: str = "kb", service_name: str = "kb-gateway", exporter: SpanExporter | None = None
    ) -> None:
        if exporter is None and (urlparse(endpoint).hostname or "") not in _LOCAL_HOSTS:
            raise ValueError(f"local.PhoenixTracer: endpoint {endpoint!r} is not a loopback address")
        super().__init__(endpoint, project, service_name, exporter)


class OtelTracer(_OtlpTracer):
    """CML: an in-cluster Phoenix or OpenTelemetry collector; https only."""

    def __init__(
        self, endpoint: str, project: str = "kb", service_name: str = "kb-gateway", exporter: SpanExporter | None = None
    ) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("cml.OtelTracer: the collector endpoint must use https")
        super().__init__(endpoint, project, service_name, exporter)


class _NullSpan:
    trace_id = ""

    def set(self, **attributes: Any) -> None:
        return None

    def error(self, message: str) -> None:
        return None


class NullTracer:
    """No tracing (offline evaluation, unit tests)."""

    STATUS = "implemented"

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[_NullSpan]:
        yield _NullSpan()

    def flush(self, timeout_ms: int = 0) -> bool:
        return True
