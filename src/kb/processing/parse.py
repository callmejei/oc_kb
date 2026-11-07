"""Parse with Docling: local models only (artifacts path), OCR for scanned pages, no remote services."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kb.config import REPO_ROOT


class ParseError(RuntimeError):
    pass


@dataclass
class ParseResult:
    document: Any  # docling_core DoclingDocument
    pages: int
    status: str
    warnings: list[str] = field(default_factory=list)


class DoclingParser:
    """Thread-safe lazy DocumentConverter. Pipelines are built once and reused."""

    def __init__(self, artifacts: str | Path, ocr_backend: str = "torch") -> None:
        p = Path(artifacts)
        self.artifacts = p if p.is_absolute() else REPO_ROOT / p
        self.ocr_backend = ocr_backend
        self._convs: dict[tuple[str, str, bool], Any] = {}  # one converter per option set, built once
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, processing: dict[str, Any]) -> DoclingParser:
        return cls(processing["docling_artifacts"], processing.get("ocr_backend", "torch"))

    @staticmethod
    def option_key(options: dict[str, Any] | None) -> tuple[str, str, bool]:
        """Normalise re-parse options (allow-listed again in the DB: kb.request_reparse)."""
        o = options or {}
        ocr = str(o.get("ocr_mode", "default"))
        table = str(o.get("table_mode", "accurate"))
        cells = str(o.get("table_cell_matching", "true")).lower() == "true"
        if ocr not in ("default", "full_page") or table not in ("accurate", "fast"):
            raise ParseError(f"invalid parse options {o}")
        return ocr, table, cells

    def _converter(self, options: dict[str, Any] | None = None) -> Any:
        key = self.option_key(options)
        with self._lock:
            if key not in self._convs:
                if not self.artifacts.exists():
                    raise ParseError(f"docling models missing at {self.artifacts}: run `kb.ps1 models`")
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import (
                    OcrMode,
                    PdfPipelineOptions,
                    RapidOcrOptions,
                    TableFormerMode,
                    TableStructureOptions,
                )
                from docling.document_converter import DocumentConverter, PdfFormatOption

                ocr, table, cells = key
                po = PdfPipelineOptions(
                    artifacts_path=self.artifacts,
                    do_ocr=True,
                    do_table_structure=True,
                    enable_remote_services=False,  # rule 2: never call hosted services
                    allow_external_plugins=False,
                    ocr_options=RapidOcrOptions(
                        backend=self.ocr_backend,  # type: ignore[arg-type]
                        mode=OcrMode.FULL_PAGE if ocr == "full_page" else OcrMode.DEFAULT,
                    ),
                    table_structure_options=TableStructureOptions(
                        mode=TableFormerMode.FAST if table == "fast" else TableFormerMode.ACCURATE,
                        do_cell_matching=cells,
                    ),
                )
                self._convs[key] = DocumentConverter(
                    allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX, InputFormat.XLSX],
                    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=po)},
                )
            return self._convs[key]

    def parse(self, path: Path, options: dict[str, Any] | None = None) -> ParseResult:
        """options (PDF only): ocr_mode default|full_page, table_mode accurate|fast, table_cell_matching."""
        from docling.datamodel.base_models import ConversionStatus

        try:
            r = self._converter(options).convert(str(path), raises_on_error=False)
        except Exception as e:  # docling raises on unreadable input before producing a result
            raise ParseError(f"{path.name}: {type(e).__name__}: {e}") from e
        warnings = [f"{e.component_type}: {e.error_message}" for e in r.errors]
        if r.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS) or r.document is None:
            raise ParseError(f"{path.name}: status {r.status}; {warnings[:3]}")
        text = r.document.export_to_text().strip()
        if not text:
            raise ParseError(f"{path.name}: no text extracted (OCR found nothing)")
        return ParseResult(
            document=r.document, pages=len(r.document.pages), status=str(r.status.value), warnings=warnings
        )
