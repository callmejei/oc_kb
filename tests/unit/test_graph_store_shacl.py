"""Graph store isolation + atomic swap, and SHACL (incl. deliberately broken fixtures)."""

from __future__ import annotations

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef

from kb.adapters.local.graph_store import OxigraphGraphStore
from kb.graph.build import MARKER, ShaclError, V, chunk_iri, entity_iri, validate, version_iri
from kb.graph.ontology import Ontology

LTV = "https://spec.edmcouncil.org/fibo/ontology/LOAN/LoansGeneral/Loans/LoanToValueRatio"


def test_query_sees_only_the_named_graphs_given(tmp_path):
    s = OxigraphGraphStore(str(tmp_path))
    s.replace_graph("urn:kb:graph:retail", iter(['<urn:e1> <urn:p> "retail secret" .']))
    s.replace_graph("urn:kb:graph:wealth", iter(['<urn:e2> <urn:p> "wealth secret" .']))
    q = "SELECT ?o WHERE { ?s ?p ?o }"
    assert [r["o"] for r in s.query(q, ["urn:kb:graph:retail"])] == ["retail secret"]
    assert {r["o"] for r in s.query(q, ["urn:kb:graph:retail", "urn:kb:graph:wealth"])} == {
        "retail secret",
        "wealth secret",
    }
    assert s.query(q, []) == []  # no scope -> nothing, never "everything"
    q_graph = "SELECT ?g ?o WHERE { GRAPH ?g { ?s ?p ?o } }"
    rows = s.query(q_graph, ["urn:kb:graph:retail"])  # GRAPH ?g must not reach outside the allowed graphs
    assert {(r["g"], r["o"]) for r in rows} == {("urn:kb:graph:retail", "retail secret")}
    named = "SELECT ?o WHERE { GRAPH <urn:kb:graph:wealth> { ?s ?p ?o } }"
    assert s.query(named, ["urn:kb:graph:retail"]) == []  # naming another BU's graph explicitly gets nothing


def test_replace_is_atomic_and_persists(tmp_path):
    s = OxigraphGraphStore(str(tmp_path))
    s.replace_graph("urn:kb:graph:retail", iter(['<urn:a> <urn:p> "v1" .', '<urn:b> <urn:p> "v1" .']))
    s.replace_graph("urn:kb:graph:retail", iter(['<urn:a> <urn:p> "v2" .']))
    assert s.count("urn:kb:graph:retail") == 1 and "urn:kb:graph:retail:staging" not in s.graphs()
    reader = OxigraphGraphStore(str(tmp_path))  # another process would see the same
    assert [r["o"] for r in reader.query("SELECT ?o WHERE { ?s ?p ?o }", ["urn:kb:graph:retail"])] == ["v2"]


# ---------------------------------------------------------------------------------------------- SHACL
@pytest.fixture(scope="module")
def onto_graph():
    o = Ontology()
    g = Graph()
    for iri in (LTV,):
        g.add((URIRef(iri), V.module, Literal(o.concepts[iri].module)))
    g.add((URIRef("https://spec.edmcouncil.org/fibo/ontology/SEC/Debt/Bonds/Bond"), V.module, Literal("SEC")))
    return g


def bu_graph(bu: str = "retail", modules=("FND", "BE", "FBC", "LOAN")) -> Graph:
    g = Graph()
    g.add((MARKER, V.graphBu, Literal(bu)))
    for m in modules:
        g.add((MARKER, V.allowedModule, Literal(m)))
    return g


def entity(g: Graph, bu: str, label: str, concept: str | None = LTV, chunk: str | None = "c1") -> URIRef:
    e = entity_iri(bu, label)
    g.add((e, RDF.type, V.Entity))
    g.add((e, RDFS.label, Literal(label)))
    g.add((e, V.bu, Literal(bu)))
    g.add((e, V.fromVersion, version_iri("d:v1")))
    if chunk:
        g.add((e, V.fromChunk, chunk_iri(chunk)))
    if concept:
        g.add((e, V.concept, URIRef(concept)))
    return e


def relation(g: Graph, s: URIRef, t: URIRef, bu: str = "retail") -> None:
    r = URIRef(f"urn:rel:{s}-{t}")
    for p, o in (
        (RDF.type, V.Relation),
        (V.source, s),
        (V.target, t),
        (V.relation, Literal("references")),
        (V.edgeTag, Literal("EXTRACTED")),
        (V.bu, Literal(bu)),
    ):
        g.add((r, p, o))


def test_valid_graph_conforms(onto_graph):
    g = bu_graph()
    relation(g, entity(g, "retail", "LTV"), entity(g, "retail", "Collateral"))
    validate(g, onto_graph)


@pytest.mark.parametrize(
    "break_it", ["cross_bu_edge", "entity_wrong_bu", "no_chunk", "disallowed_module", "unknown_concept"]
)
def test_broken_fixtures_fail(onto_graph, break_it):
    g = bu_graph()
    a = entity(g, "retail", "LTV")
    if break_it == "cross_bu_edge":
        relation(g, a, entity(g, "wealth", "Fund"))
    elif break_it == "entity_wrong_bu":
        entity(g, "wealth", "Fund")
    elif break_it == "no_chunk":
        entity(g, "retail", "Orphan", chunk=None)
    elif break_it == "disallowed_module":
        entity(g, "retail", "Bond", concept="https://spec.edmcouncil.org/fibo/ontology/SEC/Debt/Bonds/Bond")
    elif break_it == "unknown_concept":
        entity(g, "retail", "Thing", concept="urn:not-a-concept")
    with pytest.raises(ShaclError):
        validate(g, onto_graph)
