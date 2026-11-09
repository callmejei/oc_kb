"""SentenceTransformerEmbedder: local model files at a pinned revision, CPU, offline."""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kb.config import REPO_ROOT

# BGE v1.5: short retrieval queries get this instruction; passages do not.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


class ModelRevisionError(RuntimeError):
    pass


class SentenceTransformerEmbedder:
    STATUS = "implemented"
    PHASE = 2

    def __init__(
        self,
        path: str,
        dims: int,
        revision: str,
        model: str = "",
        device: str = "cpu",
        batch_size: int = 32,
        query_instruction: str = BGE_QUERY_INSTRUCTION,
        **_: Any,
    ) -> None:
        p = Path(path)
        self.path = p if p.is_absolute() else REPO_ROOT / p
        self._dims = int(dims)
        self.revision = revision
        self.model_name = model
        self.device = device
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        self._model: Any = None
        self._lock = threading.Lock()

    @property
    def dims(self) -> int:
        return self._dims

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                rev_file = self.path / ".revision"
                found = rev_file.read_text(encoding="utf-8").strip() if rev_file.exists() else "<missing>"
                if found != self.revision:
                    raise ModelRevisionError(
                        f"{self.path} is revision {found}, config pins {self.revision}: run `kb.ps1 models`"
                    )
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                from sentence_transformers import SentenceTransformer

                m = SentenceTransformer(str(self.path), device=self.device, local_files_only=True)
                if m.get_embedding_dimension() != self._dims:
                    raise ModelRevisionError(f"model dims {m.get_embedding_dimension()} != {self._dims}")
                self._model = m
            return self._model

    @property
    def tokenizer(self) -> Any:
        return self._load().tokenizer

    @property
    def max_seq_length(self) -> int:
        return int(self._load().max_seq_length)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self._load().encode(
            list(texts), batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        v = self._load().encode([self.query_instruction + text], normalize_embeddings=True, show_progress_bar=False)
        return list(v[0].tolist())
