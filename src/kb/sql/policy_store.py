"""Data-product access policies (Phase 9b, D-066): validation, row-filter sanitising, loading from the registry.

A product spec (one mart table) is ``{grants: [group], row_filters: [{groups, filter}], column_masks: {col: {mask,
unless_groups}}}``. Approved specs of ACTIVE products (view ``kb.data_policy_current``) are merged into the policy
engine's shape ``{table_grants, row_filters, column_masks}``.

Row filters are written by makers, so they are NOT trusted text: each is parsed with sqlglot and accepted only if it
is a boolean expression over this table's own columns and literals, using comparisons, IN (list), IS [NOT] NULL,
BETWEEN, AND/OR/NOT and parentheses: no subqueries, functions, casts, or other tables. The engine uses the
re-serialised AST, never the submitted string. Validation runs when a draft is saved (API) and again on every load:
a spec that fails at load denies its table (fail closed) and is reported.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

DIALECT = "duckdb"
MASKS = ("redact", "hash", "year_only", "null")
_GROUP = re.compile(r"^GRP_[A-Z0-9_]{2,60}$")
_COL = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
ALLOWED = (
    exp.Column,
    exp.Identifier,
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Paren,
    exp.Tuple,
    exp.And,
    exp.Or,
    exp.Not,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
    exp.Is,
    exp.Between,
    exp.Neg,
)


class SpecError(ValueError):
    pass


def safe_filter(text: str, columns: Sequence[str]) -> str:
    """The canonical SQL of a row filter, or SpecError. Only this table's columns; no subqueries or functions."""
    if not isinstance(text, str) or not 1 <= len(text) <= 500:
        raise SpecError("a row filter is 1-500 characters")
    try:
        tree = sqlglot.parse_one(text, read=DIALECT, into=exp.Condition)
    except Exception as e:  # noqa: BLE001 - any parse failure is a refusal
        raise SpecError(f"row filter does not parse: {str(e).splitlines()[0][:120]}") from None
    if tree is None:
        raise SpecError("empty row filter")
    cols = set(columns)
    for node in tree.walk():
        if not isinstance(node, ALLOWED):
            raise SpecError(f"row filter may not contain {type(node).__name__}")
        if node.comments:
            raise SpecError("row filter may not contain comments")
        if isinstance(node, exp.Column) and (node.table or node.name not in cols):
            raise SpecError(f"row filter column {node.sql(DIALECT)} is not a column of this table")
        if isinstance(node, exp.In) and node.args.get("query") is not None:
            raise SpecError("row filter may not contain a subquery")
    if not isinstance(tree, (exp.Connector, exp.Not, exp.Predicate, exp.Paren, exp.Boolean)):
        raise SpecError("a row filter must be a condition")
    return tree.sql(dialect=DIALECT)


def validate_spec(table: str, spec: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    """Normalised spec, or SpecError. Same rules as kb.valid_product_spec plus column and filter checks."""
    if not isinstance(spec, Mapping) or set(spec) - {"grants", "row_filters", "column_masks"}:
        raise SpecError("spec keys are grants, row_filters, column_masks")
    grants = list(spec.get("grants") or [])
    if not all(isinstance(g, str) and _GROUP.match(g) for g in grants):
        raise SpecError("grants must be GRP_* group names")
    filters = []
    for rf in spec.get("row_filters") or []:
        groups = list(rf.get("groups") or []) if isinstance(rf, Mapping) else []
        if not groups or not all(isinstance(g, str) and _GROUP.match(g) for g in groups):
            raise SpecError("each row filter needs GRP_* groups")
        filters.append({"groups": groups, "filter": safe_filter(rf.get("filter", ""), columns)})
    masks = {}
    for col, m in (spec.get("column_masks") or {}).items():
        if not _COL.match(col) or col not in columns:
            raise SpecError(f"mask on unknown column {col}")
        if not isinstance(m, Mapping) or m.get("mask") not in MASKS:
            raise SpecError(f"mask on {col} must be one of {', '.join(MASKS)}")
        unless = list(m.get("unless_groups") or [])
        if not all(isinstance(g, str) and _GROUP.match(g) for g in unless):
            raise SpecError(f"mask exemptions on {col} must be GRP_* groups")
        masks[col] = {"mask": m["mask"], "unless_groups": unless}
    return {"grants": grants, "row_filters": filters, "column_masks": masks}


def merge(specs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Per-table specs -> the policy engine's {table_grants, row_filters, column_masks}."""
    grants: dict[str, list[str]] = {}
    filters: dict[str, list[dict[str, Any]]] = {}
    masks: dict[str, dict[str, Any]] = {}
    for table, s in specs.items():
        for g in s["grants"]:
            grants.setdefault(g, []).append(table)
        filters[table] = list(s["row_filters"])
        for col, m in s["column_masks"].items():
            masks[f"{table}.{col}"] = dict(m)
    return {"table_grants": grants, "row_filters": filters, "column_masks": masks}


def specs_from_yaml(pol: Mapping[str, Any], tables: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The inverse of merge(): used once to seed products from config/policies.yaml (the first seed)."""
    out: dict[str, dict[str, Any]] = {}
    for t in tables:
        grants = sorted(g for g, ts in pol.get("table_grants", {}).items() if t in ts)
        masks = {
            k.rsplit(".", 1)[1]: {"mask": v["mask"], "unless_groups": list(v.get("unless_groups", []))}
            for k, v in pol.get("column_masks", {}).items()
            if k.rsplit(".", 1)[0] == t
        }
        out[t] = {"grants": grants, "row_filters": list(pol.get("row_filters", {}).get(t, [])), "column_masks": masks}
    return out


@dataclass
class Loaded:
    version: int
    policies: dict[str, Any]
    revisions: dict[str, str] = field(default_factory=dict)  # table -> revision id
    errors: dict[str, str] = field(default_factory=dict)  # table -> why it is denied


class RegistryPolicySource:
    """Reads the approved policy of every ACTIVE product. ``current()`` costs one tiny query (the version);
    the full policy is reloaded only when the version changes, so a restriction applies to the very next query."""

    def __init__(self, connect: Callable[[], Any], catalog: Mapping[str, Sequence[str]]) -> None:
        self._connect = connect
        self.catalog = catalog
        self._lock = threading.Lock()
        self._loaded: Loaded | None = None

    def version(self) -> int:
        with self._connect() as con:
            return int(con.execute("SELECT kb.policy_version() AS v").fetchone()["v"])

    def current(self) -> Loaded:
        v = self.version()
        with self._lock:
            if self._loaded is None or self._loaded.version != v:
                self._loaded = self._load()
            return self._loaded

    def _load(self) -> Loaded:
        with self._connect() as con:
            v = int(con.execute("SELECT kb.policy_version() AS v").fetchone()["v"])
            rows = con.execute("SELECT product_id, revision_id, spec FROM kb.data_policy_current").fetchall()
        specs, revisions, errors = {}, {}, {}
        for r in rows:
            t = r["product_id"]
            if t not in self.catalog:
                errors[t] = "no such mart table"
                continue
            try:
                specs[t] = validate_spec(t, r["spec"], self.catalog[t])
                revisions[t] = str(r["revision_id"])
            except SpecError as e:
                errors[t] = str(e)  # fail closed: the table is denied
        return Loaded(v, merge(specs), revisions, errors)
