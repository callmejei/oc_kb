"""The seeded corpus matches CLAUDE.md section 6."""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path

import pytest
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

from synthetic.sharepoint import SharePointSim, sha256_file
from synthetic.world import BUS, CANARIES


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        d = Document(str(path))
        parts = [p.text for p in d.paragraphs]
        parts += [c.text for t in d.tables for r in t.rows for c in r.cells]
        return "\n".join(parts)
    if ext == ".pptx":
        parts = []
        for s in Presentation(str(path)).slides:
            for sh in s.shapes:
                if sh.has_text_frame:
                    parts.append(sh.text_frame.text)
                if sh.has_table:
                    parts += [c.text for r in sh.table.rows for c in r.cells]
        return "\n".join(parts)
    if ext == ".xlsx":
        wb = load_workbook(path, read_only=True)
        return "\n".join(str(v) for ws in wb for row in ws.iter_rows(values_only=True) for v in row if v)
    if ext == ".pdf":
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    raise ValueError(ext)


@pytest.fixture(scope="module")
def docs(manifest, data_dir):
    return [{**d, "abs": data_dir / d["path"]} for d in manifest["documents"]]


@pytest.fixture(scope="module")
def texts(docs):
    return {d["key"]: extract_text(d["abs"]) for d in docs}


def test_about_forty_documents_across_all_bus(docs):
    assert 36 <= len(docs) <= 44
    assert set(Counter(d["bu"] for d in docs)) == set(BUS)
    assert min(Counter(d["bu"] for d in docs).values()) >= 8


def test_all_formats_present(docs):
    fmts = Counter(d["fmt"] for d in docs)
    for f in ("docx", "pdf", "pdf2col", "pdfscan", "pptx", "xlsx"):
        assert fmts[f] >= 4, fmts


def test_files_exist_and_hashes_match_manifest(docs):
    for d in docs:
        assert d["abs"].exists(), d["path"]
        assert sha256_file(d["abs"]) == d["sha256"], d["path"]


def test_office_files_are_valid_zip_containers(docs):
    for d in docs:
        if d["fmt"] in ("docx", "pptx", "xlsx"):
            assert zipfile.is_zipfile(d["abs"]), d["path"]


def test_scanned_pdfs_have_no_text_layer_and_text_pdfs_do(docs, texts):
    for d in docs:
        if d["fmt"] == "pdfscan":
            assert texts[d["key"]].strip() == "", d["path"]
        elif d["fmt"] in ("pdf", "pdf2col"):
            assert len(texts[d["key"]]) > 500, d["path"]


def test_two_column_pdfs_really_have_two_columns(docs):
    for d in docs:
        if d["fmt"] != "pdf2col":
            continue
        xs: list[float] = []
        page = PdfReader(str(d["abs"])).pages[0]
        # device x = ctm translate + text-matrix x scaled by ctm
        page.extract_text(
            visitor_text=lambda t, cm, tm, fd, fs, xs=xs: xs.append(cm[4] + tm[4] * cm[0]) if t.strip() else None
        )
        width = float(page.mediabox.width)
        assert any(x > width * 0.5 for x in xs) and any(x < width * 0.25 for x in xs), d["path"]


def test_ltv_tables_present(docs, texts):
    ltv_docs = [d for d in docs if "LTV" in texts[d["key"]] or "advance ratio" in texts[d["key"]]]
    assert len(ltv_docs) >= 5
    assert {d["bu"] for d in ltv_docs} >= {"retail", "corporate", "wealth"}


def test_every_bu_has_canaries():
    assert {c.bu for c in CANARIES} == set(BUS)


def test_canaries_only_in_their_own_bu(docs, texts):
    """Each canary secret appears in >=1 text-extractable doc of its BU (or a scanned one) and nowhere else."""
    for c in CANARIES:
        holders = {d["key"] for d in docs if c.canary_id in d["canaries"]}
        assert holders, c.canary_id
        assert {d["bu"] for d in docs if d["key"] in holders} == {c.bu}
        for d in docs:
            found = c.secret in texts[d["key"]]
            if d["key"] in holders and d["fmt"] != "pdfscan":
                assert found, (c.canary_id, d["path"])
            if d["key"] not in holders:
                assert not found, f"canary {c.canary_id} leaked into {d['path']}"


def test_canary_secrets_are_unique(manifest):
    secrets = [c["secret"] for c in manifest["canaries"]] + [
        u["secret"] for u in manifest["upload_scenarios"] if "secret" in u
    ]
    assert len(secrets) == len(set(secrets))


def test_ledger_matches_files(data_dir, docs):
    sim = SharePointSim(data_dir / "sharepoint")
    for d in docs:
        item = sim.items[d["item_id"]]
        assert not item.deleted and item.path == d["path"].removeprefix("sharepoint/")
        assert item.sha256 == d["sha256"] and item.version == 1
    assert len({i.ctag for i in sim.items.values()}) == len(sim.items)


def test_upload_scenarios_cover_section_6(manifest, data_dir):
    s = {u["scenario"]: u for u in manifest["upload_scenarios"]}
    assert set(s) == {
        "private_upload",
        "reupload_new_version",
        "publish_approved",
        "publish_rejected",
        "duplicate_of_sharepoint",
        "lying_extension",
        "forbidden_space",
    }
    dup = s["duplicate_of_sharepoint"]
    sp = next(d for d in manifest["documents"] if d["key"] == dup["duplicate_of"])
    assert dup["sha256"] == sp["sha256"]
    lying = data_dir / s["lying_extension"]["file"]
    assert lying.suffix == ".pdf" and lying.read_bytes()[:4] == b"PK\x03\x04"
    assert s["reupload_new_version"]["sha256"] != s["private_upload"]["sha256"]
    assert s["forbidden_space"]["space"] == "wealth-uploads" and s["forbidden_space"]["user"].startswith("ret.")
