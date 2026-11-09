"""File type detection from content (magic bytes + OOXML part names). No guessing from names.

Detected types: pdf, docx, pptx, xlsx, zip (other ZIP), ole2 (legacy Office / MSG / MPP), unknown.
"""

from __future__ import annotations

import zipfile
from typing import BinaryIO

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

# an OOXML package must have [Content_Types].xml and the main part of its kind
OOXML_MAIN_PARTS = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
    "xlsx": "xl/workbook.xml",
}
MAX_ZIP_ENTRIES = 10_000  # refuse zip bombs by entry count before reading any member


def detect_type(stream: BinaryIO) -> str:
    """Detect the real type of a seekable binary stream. Leaves the stream at position 0."""
    stream.seek(0)
    head = stream.read(8)
    stream.seek(0)
    try:
        if head.startswith(PDF_MAGIC):
            return "pdf"
        if head.startswith(OLE2_MAGIC):
            return "ole2"
        if head.startswith(ZIP_MAGIC):
            return _ooxml_kind(stream)
        return "unknown"
    finally:
        stream.seek(0)


def _ooxml_kind(stream: BinaryIO) -> str:
    try:
        with zipfile.ZipFile(stream) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return "zip"
            names = {i.filename for i in infos}
    except zipfile.BadZipFile:
        return "unknown"
    if "[Content_Types].xml" not in names:
        return "zip"
    kinds = [k for k, part in OOXML_MAIN_PARTS.items() if part in names]
    return kinds[0] if len(kinds) == 1 else "zip"


def declared_type(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
