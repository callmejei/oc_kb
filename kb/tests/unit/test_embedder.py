"""Local embedder: pinned revision, 384-d, normalized, runs with the network blocked."""

from __future__ import annotations

import math

import pytest

from kb.adapters import build
from kb.adapters.local.embedder import ModelRevisionError, SentenceTransformerEmbedder


@pytest.fixture(scope="module")
def emb(settings):
    return build(settings, "embedder")


def test_embeds_offline_normalized(emb, no_internet):
    docs = emb.embed_documents(["Maximum loan-to-value for investment property is 70%.", "Fees schedule"])
    q = emb.embed_query("what is the LTV for an investment property?")
    assert len(docs) == 2 and all(len(v) == emb.dims == 384 for v in [*docs, q])
    assert math.isclose(sum(x * x for x in q), 1.0, rel_tol=1e-4)
    sim = [sum(a * b for a, b in zip(q, d, strict=True)) for d in docs]
    assert sim[0] > sim[1]


def test_wrong_revision_refuses_to_load(settings):
    opts = dict(settings.ports["embedder"].options)
    opts["revision"] = "0" * 40
    with pytest.raises(ModelRevisionError, match="kb.ps1 models"):
        SentenceTransformerEmbedder(**opts).embed_query("x")
