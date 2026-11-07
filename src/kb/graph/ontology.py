"""Ontology: FIBO classes + local extension concepts, a term index, and a deterministic term spotter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kb.config import REPO_ROOT
from kb.graph.fibo import FiboClass, load_index, norm_label

KB_PREFIX = "kb:"


@dataclass
class Concept:
    iri: str
    curie: str
    label: str
    terms: list[str] = field(default_factory=list)  # normalised labels/synonyms that mean this concept
    module: str = ""  # FIBO module (local concepts inherit their broader class's module)
    broader: str | None = None  # IRI
    local: bool = False
    definition: str = ""


class OntologyError(ValueError):
    pass


class Ontology:
    def __init__(self, config_path: str | Path = "config/ontology.yaml", fibo: list[FiboClass] | None = None) -> None:
        p = Path(config_path)
        p = p if p.is_absolute() else REPO_ROOT / p
        self.cfg: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8"))
        self.ns: str = self.cfg["namespace"]
        self.thresholds: dict[str, float] = self.cfg["thresholds"]
        self.allowed_modules: dict[str, list[str]] = self.cfg["allowed_modules"]
        fibo = fibo if fibo is not None else load_index()
        self.concepts: dict[str, Concept] = {}
        curie_count: dict[str, int] = {}
        for c in fibo:
            curie_count[c.curie] = curie_count.get(c.curie, 0) + 1
            self.concepts[c.iri] = Concept(
                c.iri,
                c.curie,
                c.label,
                [norm_label(c.label)] + [norm_label(s) for s in c.synonyms],
                c.module,
                definition=c.definition,
            )
        self._by_curie = {c.curie: c.iri for c in self.concepts.values() if curie_count[c.curie] == 1}
        self._ambiguous = {k for k, n in curie_count.items() if n > 1}
        for name, spec in self.cfg.get("concepts", {}).items():
            broader = self.resolve(spec["broader"])
            iri = self.ns + name
            self.concepts[iri] = Concept(
                iri,
                f"{KB_PREFIX}{name}",
                spec["labels"][0],
                [norm_label(t) for t in spec["labels"]],
                self.concepts[broader].module,
                broader=broader,
                local=True,
            )
            self._by_curie[f"{KB_PREFIX}{name}"] = iri
        for curie, terms in self.cfg.get("synonyms", {}).items():
            self.concepts[self.resolve(curie)].terms.extend(norm_label(t) for t in terms)
        self.spot_exclude = {self.resolve(c) for c in self.cfg.get("spot_exclude", [])}
        self._build_term_index()

    # ------------------------------------------------------------------ resolution
    def resolve(self, curie_or_iri: str) -> str:
        if curie_or_iri.startswith("http"):
            if curie_or_iri not in self.concepts:
                raise OntologyError(f"unknown concept {curie_or_iri}")
            return curie_or_iri
        if curie_or_iri in self._ambiguous:
            raise OntologyError(f"ambiguous curie {curie_or_iri}; use the full IRI")
        if curie_or_iri not in self._by_curie:
            raise OntologyError(f"unknown concept {curie_or_iri}")
        return self._by_curie[curie_or_iri]

    def allowed(self, iri: str, bu: str) -> bool:
        return self.concepts[iri].module in self.allowed_modules.get(bu, [])

    # ------------------------------------------------------------------ term index / spotting
    def _build_term_index(self) -> None:
        """term -> concept. Priority: local concepts, curated synonyms, then FIBO labels. Only multi-word FIBO
        labels are used for spotting (single generic words like 'party' would tag everything)."""
        self.term_concept: dict[str, str] = {}
        curated: set[str] = set()
        for c in self.concepts.values():
            if c.local:
                for t in c.terms:
                    self.term_concept.setdefault(t, c.iri)
                    curated.add(t)
        for curie, terms in self.cfg.get("synonyms", {}).items():
            iri = self.resolve(curie)
            for t in terms:
                self.term_concept.setdefault(norm_label(t), iri)
                curated.add(norm_label(t))
        for c in self.concepts.values():
            if not c.local:
                for t in c.terms[:1]:  # the FIBO label itself
                    self.term_concept.setdefault(t, c.iri)
        spot_terms = sorted(
            (t for t in self.term_concept if t in curated or len(t.split()) >= 2), key=len, reverse=True
        )
        alts = "|".join(re.escape(t).replace(r"\ ", r"[\s\-]+") for t in spot_terms)
        self._spot_re = re.compile(r"\b(?:" + alts + r")(?:e?s)?\b", re.IGNORECASE)  # plurals: valuation(s)

    def spot(self, text: str, bu: str | None = None) -> list[str]:
        """Concepts mentioned in text (longest match wins), optionally restricted to a BU's allowed modules."""
        out: list[str] = []
        for m in self._spot_re.finditer(text):
            n = norm_label(m.group(0))
            iri = self.term_concept.get(n) or self.term_concept.get(n[:-1]) or self.term_concept.get(n[:-2])
            if iri is None or iri in self.spot_exclude or (bu and not self.allowed(iri, bu)):
                continue
            if iri not in out:
                out.append(iri)
        return out

    def expansion_terms(self, iri: str, limit: int = 8) -> list[str]:
        c = self.concepts[iri]
        terms = list(dict.fromkeys([norm_label(c.label), *c.terms]))
        return terms[:limit]
