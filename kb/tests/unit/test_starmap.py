"""Phase 7 star map artefact: built only from the pinned FIBO release + local concepts, deterministic, no BU data."""

from __future__ import annotations

import json

import pytest

from kb.graph.fibo import FIBO_ROOT
from kb.graph.starmap import E_BROADER, build

pytestmark = pytest.mark.skipif(not FIBO_ROOT.exists(), reason="FIBO release not fetched (kb.ps1 models)")


@pytest.fixture(scope="module")
def built() -> dict:  # type: ignore[type-arg]
    return build()


def test_every_module_and_no_bank_data(built):
    assert {m["id"] for m in built["modules"]} == {"FND", "BE", "FBC", "SEC", "DER", "LOAN", "IND", "BP", "CAE", "MD",
                                                   "ACTUS", "KB"}  # fmt: skip
    assert built["counts"]["C"] > 2000 and len(built["pos"]) == len(built["nodes"])
    iris = [n.get("iri", "") for n in built["nodes"]]
    assert not any(i.startswith("https://kb.local/id/") for i in iris)  # BU entities come only from the overlay API
    assert all(i.startswith(("https://spec.edmcouncil.org/fibo/", "https://kb.local/ontology/")) for i in iris if i)


def test_local_concepts_hang_off_their_broader_fibo_class(built):
    nodes = built["nodes"]
    local = [i for i, n in enumerate(nodes) if n["k"] == "K"]
    linked = {
        a for a, b, k in built["edges"] if k == E_BROADER and nodes[b]["iri"].startswith("https://spec.edmcouncil")
    }
    assert local and set(local) == linked


def test_rebuild_is_identical(built):
    again = build()
    drop = ("build_seconds",)
    assert json.dumps({k: v for k, v in built.items() if k not in drop}, sort_keys=True) == json.dumps(
        {k: v for k, v in again.items() if k not in drop}, sort_keys=True
    )
