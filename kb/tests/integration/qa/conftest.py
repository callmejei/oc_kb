"""Q&A fixtures over the REAL local deployment index (`kb.ps1 onboard` must have run)."""

from __future__ import annotations

import pytest

from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.config import load_settings
from kb.retrieval.pipeline import HybridRetriever

SOURCES = ("retail-kb", "corporate-kb", "wealth-kb", "shared-kb")


@pytest.fixture(scope="session")
def qa_settings(secrets):
    return load_settings("local", strict=True)


@pytest.fixture(scope="session")
def live_index(qa_settings):
    idx = build(qa_settings, "chunk_index")
    for s in SOURCES:
        if idx.count({"bool": {"filter": [{"term": {"source_id": s}}, {"term": {"is_current": True}}]}}) == 0:
            pytest.fail(f"live index has no current chunks for {s}: run `kb.ps1 onboard` first")
    return idx


@pytest.fixture(scope="session")
def idp():
    return YamlIdentityProvider("config/users.yaml")


@pytest.fixture(scope="session")
def retriever(qa_settings, live_index):
    return HybridRetriever(
        live_index, build(qa_settings, "embedder"), build(qa_settings, "reranker"), qa_settings.retrieval
    )
