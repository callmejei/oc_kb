"""Build the IngestEngine from config. Tests override the stores (own DB, index alias, folders)."""

from __future__ import annotations

from typing import Any

from kb.adapters import adapter_class
from kb.adapters.local.registry import PostgresRegistry
from kb.config import Settings
from kb.ingest.engine import IngestEngine
from kb.processing.parse import DoclingParser
from kb.processing.pii import PiiGate

_shared: dict[str, Any] = {}  # heavy, stateless models reused across engines in one process


def _port(settings: Settings, name: str, **override: Any) -> Any:
    pc = settings.ports[name]
    return adapter_class(pc.adapter)(**(pc.options | override))


def build_engine(
    settings: Settings,
    *,
    registry_dsn: str | None = None,
    index_alias: str | None = None,
    raw_url: str | None = None,
    processed_url: str | None = None,
    sharepoint_root: str | None = None,
) -> IngestEngine:
    proc = settings.processing
    if "embedder" not in _shared:
        _shared["embedder"] = _port(settings, "embedder")
        _shared["parser"] = DoclingParser.from_settings(proc)
        _shared["pii"] = PiiGate(proc["pii"]["spacy_model"])
    return IngestEngine(
        registry=PostgresRegistry(registry_dsn or proc["registry_dsn"]),
        sources=_port(settings, "document_source", **({"root": sharepoint_root} if sharepoint_root else {})),
        raw=_port(settings, "raw_store", **({"url": raw_url} if raw_url else {})),
        processed=_port(settings, "raw_store", url=processed_url or proc["processed_url"]),
        scanner=_port(settings, "malware_scanner"),
        index=_port(settings, "chunk_index", **({"index_alias": index_alias} if index_alias else {})),
        embedder=_shared["embedder"],
        parser=_shared["parser"],
        pii=_shared["pii"],
        processing=proc,
    )
