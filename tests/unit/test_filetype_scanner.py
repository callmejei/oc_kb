"""File type detection and the local AllowListScanner (type checks only, never claims a scan)."""

from __future__ import annotations

import io
import logging
import zipfile

import pytest

from kb.adapters.local.scanner import AllowListScanner
from kb.processing.filetype import detect_type


def test_every_corpus_file_is_detected_as_its_extension(manifest, data_dir):
    for d in manifest["documents"]:
        path = data_dir / d["path"]
        with open(path, "rb") as f:
            assert detect_type(f) == path.suffix.lstrip("."), d["path"]


def _zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "x")
    buf.seek(0)
    return buf


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (io.BytesIO(b"%PDF-1.7 ..."), "pdf"),
        (io.BytesIO(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 64), "ole2"),
        (_zip(["report.txt"]), "zip"),
        (_zip(["[Content_Types].xml", "word/document.xml"]), "docx"),
        (_zip(["[Content_Types].xml", "word/document.xml", "xl/workbook.xml"]), "zip"),  # ambiguous
        (_zip(["word/document.xml"]), "zip"),  # no content types part
        (io.BytesIO(b"MZ\x90\x00 not allowed"), "unknown"),
        (io.BytesIO(b"PK\x03\x04 truncated"), "unknown"),
    ],
)
def test_detect_type(data, expected):
    assert detect_type(data) == expected
    assert data.tell() == 0


def test_scanner_accepts_matching_type_but_never_claims_a_scan(data_dir, manifest, caplog):
    d = next(x for x in manifest["documents"] if x["fmt"] == "docx")
    with caplog.at_level(logging.WARNING, logger="kb.scanner"), open(data_dir / d["path"], "rb") as f:
        r = AllowListScanner().scan(f, d["path"])
    assert r.clean and r.scanned is False and r.detected_type == "docx"
    assert "NOT performed" in caplog.text


def test_scanner_rejects_lying_extension(manifest, data_dir):
    s = next(u for u in manifest["upload_scenarios"] if u["scenario"] == "lying_extension")
    with open(data_dir / s["file"], "rb") as f:
        r = AllowListScanner().scan(f, "Quarterly_Report.pdf")
    assert not r.clean and r.detected_type == "zip" and "extension .pdf" in r.reason


def test_scanner_rejects_disallowed_types():
    r = AllowListScanner().scan(io.BytesIO(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 64), "Plan.mpp")
    assert not r.clean and r.detected_type == "ole2"
