"""Metric definitions: validate, compile to one SELECT, match a question, run as the user.

A definition (stored as jsonb, approved through maker-checker) is structured, never SQL text:

    {"label": "NPL ratio", "synonyms": ["non-performing loan ratio"], "concept": "<IRI>",
     "table": "mart_credit.loan",
     "measure": {"agg": "ratio", "scale": 100,
                 "numerator":   {"agg": "sum", "column": "outstanding", "where": "status = 'ARREARS_90'"},
                 "denominator": {"agg": "sum", "column": "outstanding", "where": "status <> 'CLOSED'"}},
     "dimensions": ["bu", "product"], "unit": "%", "columns": [...derived...]}

Compilation is deterministic; `where` conditions go through the same sanitiser as row filters. The compiled SELECT
is executed with ``SqlEngine.execute_with_decision`` AS THE USER, so the policy rewrite (grants, row filters, masks)
applies exactly as for any query. A metric whose columns are masked for the user is refused (never computed on
masked values), and the database refuses to publish a metric on columns its product masks for anyone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kb.sql.policy_store import SpecError, safe_filter

AGGS = ("sum", "avg", "min", "max", "count", "count_distinct")
UNITS = ("%", "EXB$", "count", "number")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_WORD = re.compile(r"[a-z0-9$%]+")
FILLER = frozenset(
    [
        "what",
        "is",
        "the",
        "a",
        "an",
        "our",
        "of",
        "for",
        "in",
        "on",
        "current",
        "currently",
        "total",
        "overall",
        "today",
        "now",
        "show",
        "me",
        "give",
        "tell",
        "please",
        "how",
        "much",
        "many",
        "do",
        "does",
        "we",
        "have",
        "value",
        "level",
        "by",
        "per",
        "each",
        "across",
        "split",
        "broken",
        "down",
        "rate",
    ]
)


class MetricError(ValueError):
    pass


def _simple(m: Mapping[str, Any], columns: Sequence[str]) -> tuple[dict[str, Any], set[str]]:
    agg, col = m.get("agg"), m.get("column")
    if agg not in AGGS:
        raise MetricError(f"aggregate must be one of {', '.join(AGGS)}")
    if not isinstance(col, str) or not _IDENT.match(col) or col not in columns:
        raise MetricError(f"unknown column {col!r}")
    out: dict[str, Any] = {"agg": agg, "column": col}
    used = {col}
    if m.get("where"):
        try:
            out["where"] = safe_filter(str(m["where"]), columns)
        except SpecError as e:
            raise MetricError(f"condition: {e}") from None
        used |= {c for c in columns if re.search(rf"\b{re.escape(c)}\b", out["where"])}
    return out, used


def validate(defn: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    """Normalised definition with the derived `columns` list, or MetricError."""
    label = str(defn.get("label", "")).strip()
    if not 2 <= len(label) <= 80:
        raise MetricError("label is 2-80 characters")
    synonyms = [str(s).strip() for s in defn.get("synonyms", []) if str(s).strip()][:20]
    m = defn.get("measure") or {}
    if m.get("agg") == "ratio":
        num, u1 = _simple(m.get("numerator") or {}, columns)
        den, u2 = _simple(m.get("denominator") or {}, columns)
        scale = float(m.get("scale", 1))
        if scale not in (1.0, 100.0):
            raise MetricError("ratio scale is 1 or 100")
        measure: dict[str, Any] = {"agg": "ratio", "numerator": num, "denominator": den, "scale": int(scale)}
        used = u1 | u2
    else:
        measure, used = _simple(m, columns)
    dims = [str(d) for d in defn.get("dimensions", [])]
    if len(dims) > 5 or not all(_IDENT.match(d) and d in columns for d in dims):
        raise MetricError("dimensions must be up to five columns of the table")
    unit = defn.get("unit")
    if unit not in UNITS:
        raise MetricError(f"unit must be one of {', '.join(UNITS)}")
    return {
        "label": label,
        "synonyms": synonyms,
        "concept": str(defn.get("concept") or ""),
        "table": str(defn["table"]),
        "measure": measure,
        "dimensions": dims,
        "unit": unit,
        "columns": sorted(used | set(dims)),
    }


def _expr(m: Mapping[str, Any]) -> str:
    col, cond = m["column"], m.get("where")
    filt = f" FILTER (WHERE {cond})" if cond else ""
    if m["agg"] == "count_distinct":
        return f"count(DISTINCT {col}){filt}"
    return f"{m['agg']}({col}){filt}"


def compile_metric(defn: Mapping[str, Any], group_by: Sequence[str] = ()) -> str:
    """One SELECT; identifiers come from the validated definition, conditions from the sanitiser."""
    m = defn["measure"]
    if m["agg"] == "ratio":
        value = (
            f"round(CAST({_expr(m['numerator'])} AS DOUBLE) / NULLIF({_expr(m['denominator'])}, 0)"
            f"{' * 100' if m.get('scale') == 100 else ''}, 2)"
        )
    elif m["agg"] in ("count", "count_distinct"):
        value = _expr(m)
    else:
        value = f"round({_expr(m)}, 2)"
    dims = [d for d in group_by if d in defn["dimensions"]]
    sel = ", ".join([*dims, f"{value} AS value"])
    tail = f" GROUP BY {', '.join(dims)} ORDER BY {', '.join(dims)}" if dims else ""
    return f"SELECT {sel} FROM {defn['table']}{tail}"  # noqa: S608 - validated identifiers + sanitised conditions


@dataclass(frozen=True)
class Metric:
    metric_id: str
    revision_no: int
    definition: dict[str, Any]
    description: str


@dataclass(frozen=True)
class Match:
    metric: Metric
    group_by: tuple[str, ...]
    phrase: str


def _norm(s: str) -> list[str]:
    return _WORD.findall(s.lower().replace("-", " "))


def _dim_words(d: str) -> list[list[str]]:
    base = d.replace("_", " ")
    alts = {base, base.rstrip("s") + "s", {"bu": "business unit", "risk_profile": "risk profile"}.get(d, base)}
    return [a.split() for a in alts]


def match(question: str, metrics: Sequence[Metric]) -> Match | None:
    """A metric answers the question only if (a) its label or a synonym appears as a phrase, (b) any "by/per X" names
    one of its dimensions, and (c) nothing else is asked: every remaining word is filler. Anything more specific
    (a product filter, a date, another table) is left to governed text-to-SQL, so a metric never answers a narrower
    question than the one asked."""
    words = _norm(question)
    best: Match | None = None
    for m in metrics:
        for phrase in [m.definition["label"], *m.definition.get("synonyms", [])]:
            p = _norm(phrase)
            for i in range(len(words) - len(p) + 1):
                if words[i : i + len(p)] != p:
                    continue
                rest = words[:i] + words[i + len(p) :]
                group_by: list[str] = []
                for d in m.definition["dimensions"]:
                    for dw in _dim_words(d):
                        for j in range(len(rest) - len(dw) + 1):
                            if rest[j : j + len(dw)] == dw and j > 0 and rest[j - 1] in ("by", "per", "each", "across"):
                                group_by.append(d)
                                rest = rest[:j] + rest[j + len(dw) :]
                                break
                        if d in group_by:
                            break
                if all(w in FILLER for w in rest) and (best is None or len(p) > len(_norm(best.phrase))):
                    best = Match(m, tuple(group_by), phrase)
    return best
