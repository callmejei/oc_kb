"""Phase 5 acceptance on the live world (after `kb.ps1 ontology`): the graphify-fibo definition of done.

- every BU has a named graph; zero triples whose kb:bu differs from the graph's BU; zero cross-BU edges
- SHACL passes on the loaded graphs
- >= 80% of entities mapped (auto or accepted), the rest queued for review
- chunks carry concept_ids
- graph expansion for a user of BU X never returns concepts/evidence/chunks of BU Y (every user)
- pending reviews are never used; checker decisions are audited; non-checkers are refused
- table_map hints only for permitted tables
"""

from __future__ import annotations

import json

import pytest
from rdflib import Graph

from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.config import REPO_ROOT
from kb.gateway.acl import build_access_filter, graph_scope
from kb.graph.build import ONTOLOGY_GRAPH, graph_iri, validate
from kb.graph.ontology import Ontology
from kb.graph.table_map import TableMap
from kb.retrieval.expansion import GraphExpander
from kb.retrieval.pipeline import HybridRetriever

from helpers import Api

BUS = ("retail", "corporate", "wealth", "shared")
V = "https://kb.local/vocab#"
IDP = YamlIdentityProvider("config/users.yaml")
USERS = [u.user_id for u in IDP.all_users()]


@pytest.fixture(scope="module")
def store(qa_settings):
    s = build(qa_settings, "graph_store")
    missing = [bu for bu in BUS if graph_iri(bu) not in s.graphs()]
    if missing:
        pytest.fail(f"no graph for {missing}: run `kb.ps1 ontology` first")
    return s


@pytest.fixture(scope="module")
def onto():
    return Ontology()


@pytest.fixture(scope="module")
def reports():
    """The newest report per BU (by file time: run ids are not guaranteed to sort chronologically)."""
    files = sorted((REPO_ROOT / "data" / "ontology" / "runs").glob("*/*/report.json"), key=lambda f: f.stat().st_mtime)
    return {f.parent.name: json.loads(f.read_text(encoding="utf-8")) for f in files if f.parent.name in BUS}


# ------------------------------------------------------------------------------------------- isolation
@pytest.mark.parametrize("bu", BUS)
def test_graph_holds_only_its_own_bu(store, bu):
    q = f'SELECT (COUNT(*) AS ?n) WHERE {{ ?s <{V}bu> ?b . FILTER (?b != "{bu}") }}'
    assert store.query(q, [graph_iri(bu)])[0]["n"] == "0"
    q_edges = f"""SELECT (COUNT(*) AS ?n) WHERE {{ ?r <{V}source> ?s ; <{V}target> ?t .
                 ?s <{V}bu> ?bs . ?t <{V}bu> ?bt . FILTER (?bs != ?bt) }}"""
    assert store.query(q_edges, [graph_iri(bu)])[0]["n"] == "0"
    assert int(store.query(f"SELECT (COUNT(*) AS ?n) WHERE {{ ?s a <{V}Chunk> }}", [graph_iri(bu)])[0]["n"]) > 0


def test_no_graph_mentions_another_bus_chunks(store, live_index):
    """Every chunk node in BU X's graph is a chunk the index stamps as BU X."""
    for bu in BUS:
        rows = store.query(f"SELECT ?c WHERE {{ ?c a <{V}Chunk> }}", [graph_iri(bu)])
        ids = [r["c"].removeprefix("urn:kb:chunk:") for r in rows]
        docs = live_index.docs({"terms": {"chunk_id": ids}}, size=5000)
        assert docs and {d["bu"] for d in docs} == {bu}, bu


@pytest.mark.parametrize("bu", BUS)
def test_loaded_graphs_pass_shacl(store, bu, onto):
    from kb.graph.build import OntologyBuilder

    data, ont = Graph(), Graph()
    nq = store.file.read_text(encoding="utf-8")
    for line in nq.splitlines():
        if line.endswith(f"<{graph_iri(bu)}> ."):
            data.parse(data=line.rsplit("<", 1)[0] + ".", format="nt")
        elif line.endswith(f"<{ONTOLOGY_GRAPH}> ."):
            ont.parse(data=line.rsplit("<", 1)[0] + ".", format="nt")
    assert len(data) > 0
    validate(data, ont)
    del OntologyBuilder


def test_mapping_coverage_at_least_80_percent(reports):
    assert set(reports) == set(BUS)
    ent = sum(r["concept_entities"] for r in reports.values())
    mapped = sum(r["auto_accepted"] + r["review_accepted"] for r in reports.values())
    queued = sum(r["queued"] for r in reports.values())
    assert ent > 0 and mapped / ent >= 0.8, (mapped, ent)
    assert mapped + queued >= ent * 0.95  # the rest is in the review queue (a few may have no candidate)


def test_chunks_carry_concept_ids(live_index):
    tagged = live_index.count(
        {"bool": {"filter": [{"exists": {"field": "concept_ids"}}, {"term": {"is_current": True}}]}}
    )
    total = live_index.count({"term": {"is_current": True}})
    assert tagged / total >= 0.5, (tagged, total)


# ------------------------------------------------------------------------------------------- expansion security
@pytest.fixture(scope="module")
def expander(store, onto):
    return GraphExpander(store, onto)


@pytest.mark.parametrize("user", USERS)
def test_expansion_never_crosses_bus(user, store, expander, retriever, live_index, manifest):
    ident = IDP.resolve(user)
    scope = graph_scope(ident, IDP.readable_bus(ident))
    allowed_graphs = {graph_iri(b) for b in scope} | {ONTOLOGY_GRAPH}
    all_labels = {
        bu: {
            r["l"]
            for r in store.query(
                f"SELECT ?l WHERE {{ ?e a <{V}Entity> ; <http://www.w3.org/2000/01/rdf-schema#label> ?l }}",
                [graph_iri(bu)],
            )
        }
        for bu in BUS
    }
    in_scope = set().union(*(all_labels[b] for b in scope)) if scope else set()
    forbidden = set().union(*(all_labels[b] for b in BUS if b not in scope)) - in_scope  # only outside scope
    questions = [c["question"] for c in manifest["canaries"]] + [
        "maximum loan to value for buy-to-let",
        "late payment fee",
        "blue chip shares advance",
        "credit files retention",
        "mortgage campaign codename",
    ]
    hybrid = HybridRetriever(live_index, retriever.embedder, None, {}, expander)  # type: ignore[arg-type]
    flt = build_access_filter(ident)
    for q in questions:
        exp = expander.expand(q, scope)
        assert set(exp.graphs) <= allowed_graphs
        assert not (set(exp.evidence) & forbidden), (user, q, set(exp.evidence) & forbidden)
        for ch in hybrid.candidates(q, flt, k=50, scope=scope):  # expansion branch included
            assert set(ch.chunk.acl_groups) & ident.acl_principals, (user, q)
            assert ch.chunk.bu in scope, (user, q, ch.chunk.bu)


def test_canary_concept_never_reaches_other_bus(store, expander):
    """graphify extracted 'COPPERFINCH ...' as a retail entity: it must stay inside the retail graph."""
    holders = [
        bu
        for bu in BUS
        if store.query(
            "SELECT ?e WHERE { ?e <http://www.w3.org/2000/01/rdf-schema#label> ?l . "
            'FILTER CONTAINS(UCASE(?l), "COPPERFINCH") }',
            [graph_iri(bu)],
        )
    ]
    assert set(holders) <= {"retail"}
    for user in ("corp.user1", "wlth.user1", "grp.reader", "nobody"):
        ident = IDP.resolve(user)
        exp = expander.expand(
            "What is the 2027 mortgage campaign codename?", graph_scope(ident, IDP.readable_bus(ident))
        )
        assert not any("COPPERFINCH" in e.upper() for e in exp.evidence), user


# ------------------------------------------------------------------------------------------- review queue
def test_pending_reviews_are_not_used_and_decisions_are_audited(api: Api, store, as_admin, regdb):
    import psycopg

    from kb.registry import migrate

    from helpers import owner_session

    with owner_session(regdb, "svc:ontology") as c:
        c.execute("""INSERT INTO kb.concept_review (bu, entity_iri, entity_label, proposed_concept, proposed_label,
                     confidence, method) VALUES ('retail', 'https://kb.local/id/retail/test-entity', 'Test entity',
                     'https://spec.edmcouncil.org/fibo/ontology/LOAN/LoansSpecific/MortgageLoans/Mortgage',
                     'mortgage', 0.7, 'embedding') ON CONFLICT DO NOTHING""")
    pending = [
        r for r in api.call("GET", "/concept-reviews", "ret.checker").json() if r["entity_iri"].endswith("test-entity")
    ]
    assert pending
    rid = pending[0]["review_id"]
    assert api.call("GET", "/concept-reviews", "ret.user1").status_code == 403
    assert api.call("POST", f"/concept-reviews/{rid}/accept", "corp.checker", {}).status_code == 403
    assert api.call("POST", f"/concept-reviews/{rid}/reject", "ret.checker", {}).status_code == 422
    r = api.call("POST", f"/concept-reviews/{rid}/accept", "ret.checker", {"reason": "correct"})
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED" and r.json()["decided_by"] == "ret.checker"
    assert api.call("POST", f"/concept-reviews/{rid}/reject", "ret.checker", {"reason": "x"}).status_code == 403
    with as_admin() as c:
        a = c.execute(
            "SELECT actor_id FROM kb.audit_log WHERE object_type = 'concept_review' AND object_id = %s", (rid,)
        ).fetchone()
    assert a["actor_id"] == "ret.checker"
    # live graphs (real registry): a concept attached via closeMatch exists only for ACCEPTED reviews;
    # PENDING proposals are never attached, so they cannot steer expansion
    with psycopg.connect(**migrate.admin_conninfo()) as c:
        accepted = {
            (r[0], r[1])
            for r in c.execute("SELECT entity_iri, proposed_concept FROM kb.concept_review WHERE status = 'ACCEPTED'")
        }
        pending = {
            (r[0], r[1])
            for r in c.execute("SELECT entity_iri, proposed_concept FROM kb.concept_review WHERE status = 'PENDING'")
        }
    for bu in BUS:
        rows = store.query(
            f"""SELECT ?e ?c WHERE {{ ?e <{V}concept> ?c .
                           FILTER NOT EXISTS {{ ?e <http://www.w3.org/2004/02/skos/core#exactMatch> ?c }} }}""",
            [graph_iri(bu)],
        )
        attached = {(r["e"], r["c"]) for r in rows}
        assert attached <= accepted, (bu, attached - accepted)
        assert not (attached & pending)


# ------------------------------------------------------------------------------------------- table_map
def test_table_map_hints_only_for_permitted_tables(qa_settings, onto):
    from kb.sql.text_to_sql import TextToSql

    class Capture:
        def __init__(self) -> None:
            self.calls = []

        def chat(self, messages, **_):  # type: ignore[no-untyped-def]
            self.calls.append(messages)
            return "-- NONE"

    llm = Capture()
    t2s = TextToSql(build(qa_settings, "sql_engine"), llm, table_map=TableMap(onto))
    t2s.run("average loan to value of collateral and total assets under management", IDP.resolve("ret.user1"))
    system = llm.calls[-1][0].content
    assert "ltv_pct" in system and "Concept hints" in system and "aum" not in system.split("Concept hints")[1]
    t2s.run("total assets under management", IDP.resolve("wlth.user1"))
    assert "aum" in llm.calls[-1][0].content.split("Concept hints")[1]
