"""Adapter catalogue and factory. config ``ports.<name>.adapter`` -> class."""

from __future__ import annotations

import importlib
from typing import Any

from kb.adapters._stub import make_stub
from kb.config import Settings
from kb.ports import (
    LLM,
    PORT_PROTOCOLS,
    DocumentSourceFactory,
    Embedder,
    GraphStore,
    IdentityProvider,
    MalwareScanner,
    Reranker,
    SqlEngine,
    Tracer,
)

# implemented adapters: "module:Class"
_IMPLEMENTED: dict[str, str] = {
    "local.YamlIdentityProvider": "kb.adapters.local.identity:YamlIdentityProvider",
    "local.FsspecRawStore": "kb.adapters.local.raw_store:FsspecRawStore",
    "local.PostgresRegistry": "kb.adapters.local.registry:PostgresRegistry",
    "local.SentenceTransformerEmbedder": "kb.adapters.local.embedder:SentenceTransformerEmbedder",
    "local.ElasticsearchChunkIndex": "kb.adapters.local.es_index:ElasticsearchChunkIndex",
    "local.AllowListScanner": "kb.adapters.local.scanner:AllowListScanner",
    "local.LocalFolderSource": "kb.adapters.local.folder_source:LocalFolderSource",
}

# stubs: name -> (protocol, phase that implements it)
_STUBS: dict[str, tuple[type, int]] = {
    "local.UploadSource": (DocumentSourceFactory, 2),
    "local.CrossEncoderReranker": (Reranker, 3),
    "local.OpenAICompatibleLLM": (LLM, 3),
    "local.DuckDbPolicySqlEngine": (SqlEngine, 4),
    "local.OxigraphGraphStore": (GraphStore, 5),
    "local.PhoenixTracer": (Tracer, 7),
    "cml.SharePointGraphSource": (DocumentSourceFactory, 7),
    "cml.BankAvScanner": (MalwareScanner, 7),
    "cml.RayServeEmbedder": (Embedder, 7),
    "cml.RayServeReranker": (Reranker, 7),
    "cml.StarburstSqlEngine": (SqlEngine, 7),
    "cml.LdapIdentityProvider": (IdentityProvider, 7),
}


def adapter_class(name: str) -> type:
    if name in _IMPLEMENTED:
        mod, cls = _IMPLEMENTED[name].split(":")
        return getattr(importlib.import_module(mod), cls)  # type: ignore[no-any-return]
    if name in _STUBS:
        proto, phase = _STUBS[name]
        return make_stub(name.split(".", 1)[1], proto, phase)
    raise KeyError(f"unknown adapter {name!r}")


def known_adapters() -> list[str]:
    return sorted({*_IMPLEMENTED, *_STUBS})


def build(settings: Settings, port: str) -> Any:
    """Instantiate the adapter configured for ``port`` and check it satisfies the Protocol."""
    pc = settings.ports[port]
    obj = adapter_class(pc.adapter)(**pc.options)
    proto = PORT_PROTOCOLS[port]
    if not isinstance(obj, proto):
        raise TypeError(f"{pc.adapter} does not satisfy {proto.__name__}")
    return obj
