"""Parse -> chunk -> PII gate on real synthetic files, fully offline."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kb.adapters import build
from kb.processing.chunk import chunk_document, chunk_id
from kb.processing.parse import DoclingParser, ParseError
from kb.processing.pii import PiiGate, PiiPolicy
from synthetic import scenarios
from synthetic.world import DOC_BY_KEY

SAMPLE = {
    "docx": "ret-home-loan-policy",
    "pdf": "ret-credit-card-sheet",
    "pdf2col": "ret-home-loan-sheet",
    "pdfscan": "ret-collections-proc",
    "pptx": "ret-q3-briefing",
    "xlsx": "corp-pricing-grid",
}


@pytest.fixture(scope="module")
def parser(settings):
    return DoclingParser.from_settings(settings.processing)


@pytest.fixture(scope="module")
def emb(settings):
    return build(settings, "embedder")


@pytest.fixture(scope="module")
def parsed(parser, emb, settings, data_dir):
    out = {}
    for fmt, key in SAMPLE.items():
        r = parser.parse(data_dir / "sharepoint" / DOC_BY_KEY[key].rel_path)
        out[fmt] = (r, chunk_document(r.document, emb.tokenizer, settings.processing["chunk_max_tokens"]))
    return out


@pytest.fixture(scope="module")
def gate(settings):
    return PiiGate(settings.processing["pii"]["spacy_model"])


@pytest.mark.parametrize("fmt", list(SAMPLE))
def test_every_format_yields_chunks_with_sections_and_budget(parsed, emb, settings, fmt):
    _, chunks = parsed[fmt]
    assert chunks, fmt
    budget = settings.processing["chunk_max_tokens"]
    assert all(len(emb.tokenizer.encode(c.text, add_special_tokens=False)) <= budget for c in chunks)
    assert [c.chunk_no for c in chunks] == list(range(len(chunks)))
    if fmt != "xlsx":  # spreadsheets have sheets, not headings
        assert all(c.section_path for c in chunks), fmt


@pytest.mark.parametrize("fmt", ["pdf", "pdf2col", "pdfscan", "pptx", "xlsx"])
def test_paginated_formats_carry_pages_and_boxes(parsed, fmt):
    _, chunks = parsed[fmt]
    assert all(c.page_start and c.page_end and c.page_start <= c.page_end for c in chunks), fmt
    if fmt.startswith("pdf"):
        assert any(i.get("bbox") for c in chunks for i in c.doc_items)


def test_docx_has_no_pages_so_citations_use_sections(parsed):
    _, chunks = parsed["docx"]
    assert all(c.page_start is None for c in chunks) and all(c.section_path for c in chunks)


def test_scanned_pdf_is_ocrd_offline(parser, data_dir, no_internet):
    r = parser.parse(data_dir / "sharepoint" / DOC_BY_KEY["ret-collections-proc"].rel_path)
    assert "QZ-5519-PLOVER" in r.document.export_to_text()


def test_canaries_survive_into_chunks(parsed):
    texts = {fmt: " ".join(c.text for c in ch) for fmt, (_, ch) in parsed.items()}
    assert "COPPERFINCH" in texts["docx"]
    assert "KESTREL" in texts["pptx"]
    assert "OSPREY" in texts["xlsx"]
    assert "PLOVER" in texts["pdfscan"]


def test_ltv_table_is_not_split_across_chunks(parsed):
    """The whole LTV table (caption + every first-facility value) lands in exactly one chunk.
    (PDF cells may wrap words, e.g. 'Owner-occupie d', so match on caption and values.)"""
    for fmt in ("docx", "pdf2col"):
        _, chunks = parsed[fmt]
        holders = [c for c in chunks if "75%" in c.text and "LTV" in c.text]
        assert len(holders) == 1, fmt
        body = holders[0].text
        assert all(v in body for v in ("75%", "70%", "65%", "60%")), fmt


def test_chunk_ids_are_deterministic():
    assert chunk_id("d:v1", 3) == chunk_id("d:v1", 3) != chunk_id("d:v2", 3)


def test_corrupt_pdf_is_a_parse_error(parser, tmp_path, data_dir):
    sp = tmp_path / "sp"
    shutil.copytree(data_dir / "sharepoint", sp)
    out = scenarios.add_corrupt(sp, "retail")
    with pytest.raises(ParseError):
        parser.parse(sp / out["path"])


def test_pii_gate_passes_clean_documents(parsed, gate, settings):
    policy = PiiPolicy.merged(settings.processing["pii"], None)
    for fmt, (r, _) in parsed.items():
        rep = gate.check(r.document.export_to_text(), policy)
        assert not rep.exceeded, (fmt, rep.counts)


def test_pii_gate_stops_pii_heavy_document(parser, gate, settings, tmp_path, data_dir, no_internet):
    sp = tmp_path / "sp"
    shutil.copytree(data_dir / "sharepoint", sp)
    out = scenarios.add_pii_heavy(sp, "retail")
    r = parser.parse(Path(sp / out["path"]))
    policy = PiiPolicy.merged(settings.processing["pii"], None)
    rep = gate.check(r.document.export_to_text(), policy)
    assert rep.exceeded and rep.counts.get("EMAIL_ADDRESS", 0) >= 10 and rep.counts.get("CREDIT_CARD", 0) >= 10
    lenient = PiiPolicy.merged(settings.processing["pii"], {"max_entities": 1000})
    assert not gate.check(r.document.export_to_text(), lenient).exceeded  # source policy overrides default
