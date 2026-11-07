"""The golden set is honest: every expected fact appears in at least one of its expected documents, and
expected documents belong to the BU whose user asks."""

from __future__ import annotations

import pytest

from kb.adapters.local.identity import YamlIdentityProvider
from kb.config import load_settings
from kb.evaluation.golden import EXPANSION, load_golden
from synthetic.extract import extract_text
from synthetic.world import DOC_BY_KEY

GOLDEN = load_golden()
EXPANSION_SET = load_golden(EXPANSION)
IDP = YamlIdentityProvider("config/users.yaml")
SP = load_settings().path("data_dir") / "sharepoint"
CASES = [(bu, q, block["user"]) for g in (GOLDEN, EXPANSION_SET) for bu, block in g.items() for q in block["questions"]]


@pytest.mark.parametrize(("bu", "q", "user"), CASES, ids=[c[1]["id"] for c in CASES])
def test_fact_is_in_an_expected_document(bu, q, user):
    # the real generated files (v1), so writer-only content (e.g. XLSX schedules) counts
    texts = [extract_text(SP / DOC_BY_KEY[k].rel_path).lower() for k in q["expected_docs"]]
    assert any(f.lower() in t for f in q["facts"] for t in texts), q["id"]
    assert all(DOC_BY_KEY[k].bu == bu for k in q["expected_docs"])
    assert bu in IDP.readable_bus(IDP.resolve(user))


def test_ids_unique_and_every_bu_covered():
    ids = [q["id"] for _, q, _ in CASES]
    assert len(ids) == len(set(ids)) and set(GOLDEN) == {"retail", "corporate", "wealth", "shared"}
