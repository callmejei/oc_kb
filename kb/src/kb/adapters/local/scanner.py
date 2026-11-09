"""AllowListScanner: type agreement only. It does NOT scan for malware and says so on every call.

On CML the bank's AV service replaces this adapter. Locally every result carries ``scanned=False``
and a WARNING is logged, so nobody mistakes a local run for a scanned one.
"""

from __future__ import annotations

import logging
from typing import Any, BinaryIO

from kb.ports.types import ScanResult
from kb.processing.filetype import declared_type, detect_type

log = logging.getLogger("kb.scanner")

KNOWN_TYPES = frozenset({"pdf", "docx", "pptx", "xlsx"})


class AllowListScanner:
    STATUS = "implemented"
    PHASE = 2

    def __init__(self, **_: Any) -> None:
        pass

    def scan(self, stream: BinaryIO, declared_name: str) -> ScanResult:
        detected = detect_type(stream)
        declared = declared_type(declared_name)
        log.warning("malware scan NOT performed for %r (local AllowListScanner: type checks only)", declared_name)
        if detected != declared:
            return ScanResult(
                clean=False,
                scanned=False,
                detected_type=detected,
                reason=f"extension .{declared} but content is {detected}",
            )
        if detected not in KNOWN_TYPES:
            return ScanResult(clean=False, scanned=False, detected_type=detected, reason=f"type {detected} not allowed")
        return ScanResult(clean=True, scanned=False, detected_type=detected, reason="not scanned")
