"""Plain-text extraction from the synthetic files (for tests and evaluation; not the ingestion parser)."""

from __future__ import annotations

from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader


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
