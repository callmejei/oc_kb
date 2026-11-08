"""Ontology config, term spotting, entity mapping rules and table_map (no LLM, no services)."""

from __future__ import annotations

import pytest
import yaml

from kb.adapters import build
from kb.config import CONFIG_DIR
from kb.graph.mapping import Mapper
from kb.graph.ontology import Ontology, OntologyError
from kb.graph.table_map import TableMap


@pytest.fixture(scope="module")
def onto():
    return Ontology()


def test_every_configured_concept_resolves(onto):
    cfg = yaml.safe_load((CONFIG_DIR / "ontology.yaml").read_text())
    for curie in [*cfg["synonyms"], *cfg["spot_exclude"], *(s["broader"] for s in cfg["concepts"].values())]:
        assert onto.resolve(curie).startswith("https://")
    for name in cfg["concepts"]:
        c = onto.concepts[onto.resolve(f"kb:{name}")]
        assert c.local and c.broader and c.module


def test_ambiguous_and_unknown_curies_are_refused(onto):
    with pytest.raises(OntologyError, match="ambiguous"):
        onto.resolve("fibo-fnd:TransactionEvent")
    with pytest.raises(OntologyError, match="unknown"):
        onto.resolve("fibo-loan:NoSuchThing")


def test_spotting_handles_synonyms_plurals_and_exclusions(onto):
    got = [
        onto.concepts[i].curie
        for i in onto.spot(
            "Two independent valuations for buy-to-let homes; LTV 70%; blue chip shares; the policy for the customer."
        )
    ]
    assert got == [
        "fibo-loan:CollateralValuation",
        "kb:InvestmentResidentialProperty",
        "fibo-loan:LoanToValueRatio",
        "fibo-fbc:EquityInstrument",
    ]  # 'policy' / 'customer' are excluded as too generic


def test_spotting_respects_bu_modules(onto):
    text = "investment-grade bonds and structured notes"
    assert "fibo-sec:Bond" in [onto.concepts[i].curie for i in onto.spot(text, "wealth")]
    assert "fibo-sec:Bond" not in [onto.concepts[i].curie for i in onto.spot(text, "retail")]  # SEC not allowed


@pytest.mark.parametrize(
    ("label", "curie", "method", "conf"),
    [
        ("Loan-to-Value Ratio", "fibo-loan:LoanToValueRatio", "exact_label", 1.0),
        ("Investment residential", "kb:InvestmentResidentialProperty", "exact_synonym", 0.95),
        ("Annual Document Retrieval Fee", "kb:DocumentRetrievalFee", "head_term", 0.88),
        ("COPPERFINCH Mortgage Campaign", "fibo-loan:Mortgage", "partial_term", 0.75),
    ],
)
def test_mapping_rules_without_embeddings(onto, label, curie, method, conf):
    m = Mapper(onto).map(label, "retail")
    assert (onto.concepts[m.concept].curie, m.method, m.confidence) == (curie, method, conf)
    assert m.accepted(onto.thresholds["auto_accept"]) is (conf >= 0.85)


def test_table_map_covers_every_mart_table_and_filters_by_grant(onto, settings):
    tm = TableMap(onto)
    engine = build(settings, "sql_engine")
    assert tm.tables() == set(engine.policy.catalog)
    concepts = onto.spot("loan to value of collateral and assets under management")
    retail = tm.hints(concepts, ["mart_credit.loan", "mart_credit.collateral", "mart_cust.customer"])
    assert any("ltv_pct" in h for h in retail) and not any("aum" in h for h in retail)
    assert tm.hints(concepts, []) == []


def test_markdown_split_keeps_blocks_and_maps_parts_back_to_the_document():
    from kb.graph.build import doc_id_of, split_markdown

    table = "| LTV | 70% |\n|---|---|\n| first | 70% |"
    md = "# A\n\nintro " + "x" * 900 + "\n\n## B\n\n" + table + "\n\n## C\n\n" + "y" * 900
    parts = split_markdown(md, 1000)
    assert len(parts) == 2 and all(len(p) <= 1000 for p in parts)
    assert any(table in p for p in parts)  # a table is never cut
    assert all(p.lstrip().startswith("#") for p in parts)  # parts start at headings
    assert "".join(parts).replace("\n", "") == md.replace("\n", "")  # nothing lost or reordered
    assert split_markdown("z" * 5000, 1000) == ["z" * 5000]  # an oversized block stays whole
    assert doc_id_of("abc123__p2.md") == "abc123" == doc_id_of(r"sub\abc123__p10.md") == doc_id_of("abc123.md")
    assert doc_id_of("my__plan.md") == "my__plan"  # only a numeric part suffix is stripped


@pytest.mark.parametrize(
    ("label", "bad"),
    [
        ("Loan-to-Value Ratio", False),
        ("Maximum LTV for investment residential property on a first facility", False),
        ("Documentation and Documentation and Documentation and Documentation", True),
        ("Exception Register and Rationale and Approver and Date and Rationale and Approver and Date", True),
        ("x" * 101, True),
    ],
)
def test_degenerate_labels_from_llm_loops_are_detected(label, bad):
    from kb.graph.build import degenerate

    assert degenerate(label) is bad


def test_embedding_matches_are_never_auto_accepted():
    from kb.graph.mapping import Mapping

    assert not Mapping("https://x", 0.99, "embedding").accepted(0.85)
    assert Mapping("https://x", 0.88, "head_term").accepted(0.85)
