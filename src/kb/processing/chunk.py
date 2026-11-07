"""Chunk a DoclingDocument with HybridChunker using the embedder's own tokenizer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChunkDraft:
    chunk_no: int
    text: str  # heading-contextualized text: what is embedded, searched and shown
    section_path: str | None
    page_start: int | None
    page_end: int | None
    doc_items: list[dict[str, Any]] = field(default_factory=list)  # refs + page + bbox for the inspector

    @property
    def content_sha(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def chunk_id(version_id: str, chunk_no: int) -> str:
    """Deterministic: re-processing the same version yields the same ids (idempotent upserts)."""
    return hashlib.sha256(f"{version_id}#{chunk_no}".encode()).hexdigest()[:32]


def chunk_document(doc: Any, tokenizer: Any, max_tokens: int) -> list[ChunkDraft]:
    from docling_core.transforms.chunker.doc_chunk import DocChunk
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

    chunker = HybridChunker(
        tokenizer=HuggingFaceTokenizer(tokenizer=tokenizer, max_tokens=max_tokens),
        merge_peers=True,
        repeat_table_header=True,
    )
    out: list[ChunkDraft] = []
    for n, base in enumerate(chunker.chunk(dl_doc=doc)):
        ch = DocChunk.model_validate(base)  # typed access to doc_items / headings
        pages: list[int] = []
        items: list[dict[str, Any]] = []
        for it in ch.meta.doc_items:
            for prov in getattr(it, "prov", []) or []:
                pages.append(prov.page_no)
                b = prov.bbox
                items.append(
                    {
                        "ref": it.self_ref,
                        "label": str(it.label),
                        "page": prov.page_no,
                        "bbox": [round(b.l, 1), round(b.t, 1), round(b.r, 1), round(b.b, 1)],
                    }
                )
            if not getattr(it, "prov", None):
                items.append({"ref": it.self_ref, "label": str(it.label)})
        headings = ch.meta.headings or []
        out.append(
            ChunkDraft(
                chunk_no=n,
                text=chunker.contextualize(chunk=ch),
                section_path=" > ".join(headings) if headings else None,
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                doc_items=items,
            )
        )
    return out
