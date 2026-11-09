"""PII gate with Presidio + a local spaCy model. Counts entities; a document over the threshold is an
exception for a checker, never silently indexed."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PiiPolicy:
    min_score: float = 0.6
    max_entities: int = 5
    entities: tuple[str, ...] = ("EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE")

    @classmethod
    def merged(cls, default: dict[str, Any], override: dict[str, Any] | None) -> PiiPolicy:
        cfg = {**default, **(override or {})}
        return cls(float(cfg["min_score"]), int(cfg["max_entities"]), tuple(cfg["entities"]))


@dataclass(frozen=True)
class PiiReport:
    counts: dict[str, int] = field(default_factory=dict)
    exceeded: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def as_detail(self, policy: PiiPolicy) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "total": self.total,
            "max_entities": policy.max_entities,
            "min_score": policy.min_score,
        }


class PiiGate:
    def __init__(self, spacy_model: str = "en_core_web_lg") -> None:
        self.spacy_model = spacy_model
        self._analyzer: Any = None
        self._lock = threading.Lock()

    def _engine(self) -> Any:
        with self._lock:
            if self._analyzer is None:
                from presidio_analyzer import AnalyzerEngine
                from presidio_analyzer.nlp_engine import NlpEngineProvider

                nlp = NlpEngineProvider(
                    nlp_configuration={
                        "nlp_engine_name": "spacy",
                        "models": [{"lang_code": "en", "model_name": self.spacy_model}],
                    }
                ).create_engine()
                self._analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
            return self._analyzer

    def check(self, text: str, policy: PiiPolicy) -> PiiReport:
        results = self._engine().analyze(
            text=text, language="en", entities=list(policy.entities), score_threshold=policy.min_score
        )
        counts = Counter(r.entity_type for r in results)
        report = dict(sorted(counts.items()))
        return PiiReport(counts=report, exceeded=sum(counts.values()) > policy.max_entities)
