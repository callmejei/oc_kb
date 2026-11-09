"""CrossEncoderReranker: bge-reranker-base from local files at a pinned revision, CPU, offline."""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from kb.adapters.local.embedder import ModelRevisionError
from kb.config import REPO_ROOT
from kb.ports.types import ScoredChunk


class CrossEncoderReranker:
    STATUS = "implemented"
    PHASE = 3

    def __init__(
        self,
        path: str,
        revision: str,
        model: str = "",
        device: str = "cpu",
        batch_size: int = 16,
        max_length: int = 512,
        **_: Any,
    ) -> None:
        p = Path(path)
        self.path = p if p.is_absolute() else REPO_ROOT / p
        self.revision = revision
        self.model_name = model
        self.device = device
        self.batch_size = batch_size
        self.max_length = max_length
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                rev_file = self.path / ".revision"
                found = rev_file.read_text(encoding="utf-8").strip() if rev_file.exists() else "<missing>"
                if found != self.revision:
                    raise ModelRevisionError(f"{self.path} is revision {found}, config pins {self.revision}")
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(
                    str(self.path), device=self.device, max_length=self.max_length, local_files_only=True
                )
            return self._model

    def rerank(self, query: str, candidates: Sequence[ScoredChunk], top_k: int) -> list[ScoredChunk]:
        if not candidates:
            return []
        scores = self._load().predict(
            [(query, c.chunk.text) for c in candidates], batch_size=self.batch_size, show_progress_bar=False
        )
        ranked = sorted(zip(candidates, scores, strict=True), key=lambda x: float(x[1]), reverse=True)
        return [replace(c, score=float(s), retriever="rerank") for c, s in ranked[:top_k]]
