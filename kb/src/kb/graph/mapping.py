"""Map an extracted entity label to a FIBO (or local extension) concept, with a confidence.

Order (skill step 3): exact FIBO label -> exact synonym/local term -> head-phrase containment -> partial
term -> embedding similarity against concept labels. The best candidate wins; >= auto_accept is an
exactMatch, anything lower is a closeMatch that goes to the review queue. Only concepts allowed for the
entity's BU are candidates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from kb.graph.fibo import norm_label
from kb.graph.ontology import Ontology


@dataclass(frozen=True)
class Mapping:
    concept: str | None
    confidence: float
    method: str

    def accepted(self, threshold: float) -> bool:
        """Auto-accept only rule-based matches (label, synonym, head term). An embedding match is never
        auto-accepted, whatever its cosine: it goes to the checker queue (D-04x, user decision 2026-11-07)."""
        return self.concept is not None and self.method != "embedding" and self.confidence >= threshold


class Mapper:
    def __init__(self, ontology: Ontology, embedder: Any = None) -> None:
        self.o, self.embedder = ontology, embedder
        self._emb_terms: list[tuple[str, str]] | None = None
        self._emb_vecs: list[list[float]] | None = None

    def _embedding_index(self) -> tuple[list[tuple[str, str]], list[list[float]]]:
        if self._emb_terms is None:
            pairs = [(t, iri) for t, iri in self.o.term_concept.items()]
            self._emb_terms = pairs
            self._emb_vecs = self.embedder.embed_documents([t for t, _ in pairs]) if self.embedder else []
        return self._emb_terms, self._emb_vecs or []

    def map(self, label: str, bu: str) -> Mapping:
        n = norm_label(label)
        th = self.o.thresholds
        best = Mapping(None, 0.0, "none")
        iri = self.o.term_concept.get(n)
        if iri and self.o.allowed(iri, bu):
            c = self.o.concepts[iri]
            exact_fibo = not c.local and n == norm_label(c.label)
            return Mapping(iri, 1.0 if exact_fibo else 0.95, "exact_label" if exact_fibo else "exact_synonym")
        words = n.split()
        for i in range(len(words)):  # head phrase: the label ENDS with a known term ("document retrieval fee")
            iri = self.o.term_concept.get(" ".join(words[i:]))
            if iri and self.o.allowed(iri, bu) and (iri not in self.o.spot_exclude or i == 0):
                best = Mapping(iri, th["contained_term"], "head_term")
                break
        if best.concept is None:
            for t in sorted(self.o.term_concept, key=len, reverse=True):
                if len(t) > 3 and f" {t} " in f" {n} ":
                    iri = self.o.term_concept[t]
                    if self.o.allowed(iri, bu) and iri not in self.o.spot_exclude:
                        best = Mapping(iri, th["partial_term"], "partial_term")
                        break
        if self.embedder is not None:
            terms, vecs = self._embedding_index()
            q = self.embedder.embed_documents([n])[0]
            scored = [
                (sum(a * b for a, b in zip(q, v, strict=True)), terms[i][1])
                for i, v in enumerate(vecs)
                if self.o.allowed(terms[i][1], bu)
            ]
            if scored:
                cos, iri = max(scored)
                cos = 0.0 if math.isnan(cos) else float(cos)
                if cos > best.confidence:
                    best = Mapping(iri, round(cos, 3), "embedding")
        return best
