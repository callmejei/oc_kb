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
        self._conv: Any = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, processing: dict[str, Any]) -> DoclingParser:
        return cls(processing["docling_artifacts"], processing.get("ocr_backend", "torch"))

    def _converter(self) -> Any:
        with self._lock:
            if self._conv is None:
                if not self.artifacts.exists():
                    raise ParseError(f"docling models missing at {self.artifacts}: run `kb.ps1 models`")
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption

                po = PdfPipelineOptions(
                    artifacts_path=self.artifacts,
                    do_ocr=True,
                    do_table_structure=True,
                    enable_remote_services=False,  # rule 2: never call hosted services
                    allow_external_plugins=False,
                    ocr_options=RapidOcrOptions(backend=self.ocr_backend),  # type: ignore[arg-type]
                )
                self._conv = DocumentConverter(
                    allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX, InputFormat.XLSX],
                    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=po)},
                )
            return self._conv

    def parse(self, path: Path) -> ParseResult:
        from docling.datamodel.base_models import ConversionStatus

        try:
            r = self._converter().convert(str(path), raises_on_error=False)
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
