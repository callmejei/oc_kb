"""Graph expansion feeds the reranker the spotted concepts' ontology labels, and nothing from BU graphs."""

from __future__ import annotations

from kb.adapters.local.graph_store import OxigraphGraphStore
from kb.graph.ontology import Ontology
from kb.retrieval.expansion import Expansion, GraphExpander


def test_rerank_query_adds_only_spotted_concept_labels(tmp_path):
    store = OxigraphGraphStore(str(tmp_path))
    store.replace_graph(  # a BU entity label must never reach the rerank query
        "urn:kb:graph:retail", iter(['<urn:e1> <http://www.w3.org/2000/01/rdf-schema#label> "SECRET CODENAME" .'])
    )
    exp = GraphExpander(store, Ontology()).expand("What does a wire transfer cost?", ["retail"])
    assert "https://kb.local/ontology/TelegraphicTransferFee" in exp.concepts
    q = exp.rerank_query("What does a wire transfer cost?")
    assert q == "What does a wire transfer cost? (telegraphic transfer fee)"
    assert "SECRET" not in q


def test_rerank_query_unchanged_without_concepts_or_when_label_already_asked():
    assert Expansion().rerank_query("hello") == "hello"
    assert Expansion(labels=["late payment fee"]).rerank_query("What is the late payment fee?") == (
        "What is the late payment fee?"
    )
