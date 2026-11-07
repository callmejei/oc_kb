"""Citation support check (D-032): every figure in an answer must be backed by the source it cites.

Number presence alone is not enough (the same "40%" can sit in a different table row of another
document), so support is judged on the source *segment* holding the figure: a sentence or table row
(Docling serialises rows as "Row, Column = value."). A segment supports an answer sentence when it holds
every figure of the sentence; among supporting segments the one covering most of the sentence's content
terms wins. The cited source must be (one of) the best; otherwise the citation is re-mapped to the best
source, and if no source holds the figures at all the claim is unsupported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?%?")
_SEG = re.compile(r"(?<=[.;:!?])\s+|\n+|\s\|\s")
_WORD = re.compile(r"[a-z][a-z\-]+")
STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "of",
        "for",
        "on",
        "in",
        "is",
        "are",
        "was",
        "were",
        "be",
        "to",
        "and",
        "or",
        "with",
        "by",
        "at",
        "as",
        "from",
        "this",
        "that",
        "what",
        "which",
        "how",
        "much",
        "many",
        "does",
        "do",
        "did",
        "per",
        "its",
        "it",
        "their",
        "there",
        "than",
        "any",
        "each",
        "all",
        "can",
        "may",
        "must",
        "should",
        "will",
        "would",
        "into",
    ]
)
TIE = 0.05


def norm_number(tok: str) -> str:
    t = tok.replace(",", "").rstrip("%")
    try:
        return repr(float(t))
    except ValueError:
        return t


def figures(text: str) -> set[str]:
    return {norm_number(m.group(0)) for m in _NUM.finditer(text)}


def stem(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def terms(text: str) -> set[str]:
    return {stem(w) for w in _WORD.findall(text.lower()) if w not in STOP}


@dataclass(frozen=True)
class Support:
    ref: str | None  # best supporting source ref (None: no figures to support, or none holds them)
    score: float
    cited_ok: bool
    unsupported: bool = False  # the sentence has figures that no source holds


def best_support(sentence: str, cited: list[str], sources: dict[str, str], question: str = "") -> Support:
    figs = figures(sentence) - figures(question)  # echoing a figure from the question needs no source
    if not figs:
        return Support(cited[0] if cited else None, 1.0, True)
    want = terms(sentence)
    scores: dict[str, float] = {}
    for ref, text in sources.items():
        best = -1.0
        for seg in _SEG.split(text):
            if figs <= figures(seg):
                best = max(best, len(want & terms(seg)) / max(1, len(want)))
        if best >= 0:
            scores[ref] = best
    if not scores:
        return Support(None, 0.0, False, unsupported=True)
    top_ref = max(scores, key=lambda r: (scores[r], r in cited))
    top = scores[top_ref]
    cited_ok = any(r in scores and scores[r] >= top - TIE for r in cited)
    return Support(top_ref, top, cited_ok)
