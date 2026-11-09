"""Policy-level conflict note (D-064), computed in code, never by the model.

A group-wide limit is a ceiling; a business-unit policy may only tighten it. When the answer quotes a
percentage from one source and a source at the other policy level (group-wide vs business unit) states a
different percentage for the SAME item, a note is appended that shows both values, cites both, and names the
stricter one. The comparison is deliberately narrow so it does not fire on unrelated figures:
- a "segment" (sentence or linearised table row) qualifies only if it holds exactly one percentage;
- two segments are about the same item only if their term sets are identical (so "first facility" never
  matches "third or more facilities");
- "stricter" is decided only for limits whose direction is known (maximum/LTV -> lower, minimum -> higher);
  otherwise both values are shown without a verdict.
Only sources already supplied to the model (ACL pre-filtered) are read, so the note cannot widen access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kb.agent.support import _SEG, figures, terms

_PCT = re.compile(r"(?<![\w.\-])\d[\d,]*(?:\.\d+)?%")
_CITE = re.compile(r"\[(\d{1,3})\]")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")
_UPPER = re.compile(r"\b(?:max|maximum|up to|not exceed|cap|ceiling|ltv|loan-to-value|limit)\b", re.IGNORECASE)
_LOWER = re.compile(r"\b(?:min|minimum|at least|floor)\b", re.IGNORECASE)
_MARK = re.compile(r"[*_`]+")


@dataclass(frozen=True)
class Src:
    ref: str  # "1", "2", ...
    text: str
    bu: str
    title: str


@dataclass
class Conflict:
    item: str
    values: dict[str, list[str]] = field(default_factory=dict)  # "group-wide" / "<bu> business unit" -> refs
    by_value: dict[str, str] = field(default_factory=dict)  # ref -> value
    stricter: str | None = None

    def audit(self) -> dict[str, Any]:
        return {"item": self.item, "by_ref": self.by_value, "stricter": self.stricter}


def _level(bu: str) -> str:
    return "group-wide" if bu == "shared" else f"{bu} business unit"


def _rows(text: str) -> list[tuple[frozenset[str], str, str]]:
    """(item terms, item label, the single percentage) for each segment holding exactly one percentage."""
    out = []
    for seg in _SEG.split(_MARK.sub("", text)):
        pcts = _PCT.findall(seg)
        if len(pcts) != 1:
            continue
        label = seg.split("=")[0] if "=" in seg else seg.replace(pcts[0], "")
        key = frozenset(terms(label))
        if len(key) >= 2:
            out.append((key, " ".join(label.split()).strip(" ,.:;-"), pcts[0]))
    return out


def _num(p: str) -> float:
    return float(p.replace(",", "").rstrip("%"))


def find_conflicts(answer_text: str, sources: list[Src], question: str = "") -> list[Conflict]:
    by_ref = {s.ref: s for s in sources}
    rows = {s.ref: _rows(s.text) for s in sources}
    found: dict[frozenset[str], Conflict] = {}
    for sent in _SENT.split(answer_text):
        want = terms(sent) | terms(question)
        for ref in _CITE.findall(sent):
            src = by_ref.get(ref)
            if src is None:
                continue
            for pct in _PCT.findall(sent):
                held = [r for r in rows[ref] if figures(r[2]) == figures(pct)]
                if not held:
                    continue
                key, label, _ = max(held, key=lambda r: len(want & r[0]))
                for other in sources:
                    if (other.bu == "shared") == (src.bu == "shared"):
                        continue  # only group-wide vs business unit
                    for okey, _, opct in rows[other.ref]:
                        if okey == key and _num(opct) != _num(pct):
                            c = found.setdefault(key, Conflict(item=label))
                            c.by_value.setdefault(ref, pct)
                            c.by_value.setdefault(other.ref, opct)
    for key, c in found.items():
        for s in sources:  # every supplied source stating this item, so all holders are cited
            for k, _, p in rows[s.ref]:
                if k == key and s.ref not in c.by_value:
                    c.by_value[s.ref] = p
        for ref in c.by_value:
            c.values.setdefault(_level(by_ref[ref].bu), []).append(ref)
        nums = {_num(v) for v in c.by_value.values()}
        if _UPPER.search(c.item) or (_UPPER.search(question) and not _LOWER.search(c.item)):
            c.stricter = f"{min(nums):g}%"
        elif _LOWER.search(c.item) or _LOWER.search(question):
            c.stricter = f"{max(nums):g}%"
    return list(found.values())


def note_for(c: Conflict) -> str:
    parts = []
    for level, refs in c.values.items():
        vals = sorted({c.by_value[r] for r in refs}, key=_num)
        cites = "".join(f"[{r}]" for r in sorted(refs, key=int))
        parts.append(
            f"the {level} source{'s' if len(refs) > 1 else ''} set{'' if len(refs) > 1 else 's'} "
            f"{' / '.join(vals)} {cites}"
        )
    note = f"Note: policy levels differ for {c.item}: " + "; ".join(parts) + "."
    if c.stricter:
        note += f" The stricter limit, {c.stricter}, applies."
    return note
