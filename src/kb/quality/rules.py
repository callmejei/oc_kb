"""Data-quality rules: load, validate against the mart catalogue, compile to one aggregate SELECT each, run.

Safety:
- rules are structured YAML; identifiers must exist in the catalogue and match a strict pattern, and every value
  is rendered as a typed sqlglot literal, so no rule text reaches SQL unquoted;
- each compiled query is parsed back and must be exactly one SELECT returning (checked, failed);
- the engine runs it as the pipeline (whole table: quality is measured on all rows, not one user's slice) and
  returns only the two counts. Failing values and row ids are never fetched or stored.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import sqlglot
import yaml
from sqlglot import exp

from kb.config import REPO_ROOT

KINDS = ("not_null", "range", "unique", "allowed_values", "max_age", "reference")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_TABLE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_RULE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class RuleError(ValueError):
    pass


class CountRunner(Protocol):
    def check_counts(self, sql: str) -> tuple[int, int]: ...


@dataclass(frozen=True)
class Rule:
    id: str
    table: str
    column: str
    kind: str
    tolerance: float = 0.0
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    rule: Rule
    checked: int
    failed: int
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error is not None:
            return False
        return self.failed <= self.rule.tolerance * self.checked

    def row(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule.id,
            "table_name": self.rule.table,
            "rule_kind": self.rule.kind,
            "column_name": self.rule.column,
            "checked": self.checked,
            "failed": self.failed,
            "tolerance": self.rule.tolerance,
            "passed": self.passed,
            "error": self.error,
        }


def load_rules(catalog: Mapping[str, Sequence[str]], path: Path | None = None) -> tuple[list[Rule], str]:
    """Rules plus the sha256 of the file as loaded. Any invalid rule fails the whole load (no partial runs)."""
    p = path or REPO_ROOT / "config" / "dq_rules.yaml"
    raw = p.read_bytes()
    doc = yaml.safe_load(raw) or {}
    rules: list[Rule] = []
    seen: set[str] = set()
    for r in doc.get("rules", []):
        rule = validate(r, catalog)
        if rule.id in seen:
            raise RuleError(f"duplicate rule id {rule.id}")
        seen.add(rule.id)
        rules.append(rule)
    return rules, hashlib.sha256(raw).hexdigest()


def validate(r: Mapping[str, Any], catalog: Mapping[str, Sequence[str]]) -> Rule:
    rid, table, column, kind = (str(r.get(k, "")) for k in ("id", "table", "column", "kind"))
    if not _RULE_ID.match(rid):
        raise RuleError(f"bad rule id {rid!r}")
    if kind not in KINDS:
        raise RuleError(f"{rid}: unknown kind {kind!r}")
    if not _TABLE.match(table) or table not in catalog:
        raise RuleError(f"{rid}: unknown table {table!r}")
    if not _IDENT.match(column) or column not in catalog[table]:
        raise RuleError(f"{rid}: unknown column {table}.{column}")
    tol = float(r.get("tolerance", 0))
    if not 0 <= tol <= 1:
        raise RuleError(f"{rid}: tolerance must be within 0..1")
    params: dict[str, Any] = {}
    if kind == "range":
        for b in ("min", "max"):
            if b in r:
                params[b] = float(r[b])
        if not params:
            raise RuleError(f"{rid}: range needs min and/or max")
        if "min" in params and "max" in params and params["min"] > params["max"]:
            raise RuleError(f"{rid}: min > max")
    elif kind == "allowed_values":
        vals = r.get("values")
        if not isinstance(vals, list) or not vals or not all(isinstance(v, (str, int, float)) for v in vals):
            raise RuleError(f"{rid}: allowed_values needs a non-empty list of scalars")
        params["values"] = list(vals)
    elif kind == "max_age":
        days = int(r.get("max_age_days", 0))
        if days <= 0:
            raise RuleError(f"{rid}: max_age_days must be positive")
        params["max_age_days"] = days
    elif kind == "reference":
        rt, rc = str(r.get("ref_table", "")), str(r.get("ref_column", ""))
        if not _TABLE.match(rt) or rt not in catalog or not _IDENT.match(rc) or rc not in catalog[rt]:
            raise RuleError(f"{rid}: unknown reference {rt}.{rc}")
        params |= {"ref_table": rt, "ref_column": rc}
    return Rule(rid, table, column, kind, tol, params)


def _lit(v: Any) -> str:
    if isinstance(v, bool):
        raise RuleError("booleans are not rule values")
    if isinstance(v, (int, float)):
        return exp.Literal.number(v).sql("duckdb")
    return exp.Literal.string(str(v)).sql("duckdb")


def compile_rule(rule: Rule, as_of: date) -> str:
    c, t, p = rule.column, rule.table, rule.params
    if rule.kind == "not_null":
        bad = f"{c} IS NULL"
    elif rule.kind == "range":
        parts = []
        if "min" in p:
            parts.append(f"{c} < {_lit(p['min'])}")
        if "max" in p:
            parts.append(f"{c} > {_lit(p['max'])}")
        bad = " OR ".join(parts)
    elif rule.kind == "allowed_values":
        bad = f"{c} IS NOT NULL AND {c} NOT IN ({', '.join(_lit(v) for v in p['values'])})"
    elif rule.kind == "max_age":
        bad = f"{c} IS NULL OR {c} < DATE {_lit(as_of.isoformat())} - INTERVAL {int(p['max_age_days'])} DAY"
    elif rule.kind == "reference":
        rc, rt = p["ref_column"], p["ref_table"]  # validated identifiers (catalogue + pattern)
        bad = f"{c} IS NOT NULL AND {c} NOT IN (SELECT {rc} FROM {rt} WHERE {rc} IS NOT NULL)"  # noqa: S608
    elif rule.kind == "unique":
        sql = f"SELECT count(*) AS checked, count({c}) - count(DISTINCT {c}) AS failed FROM {t}"  # noqa: S608
        return _checked(sql)
    else:  # pragma: no cover - validate() rejects unknown kinds
        raise RuleError(rule.kind)
    return _checked(f"SELECT count(*) AS checked, count(*) FILTER (WHERE {bad}) AS failed FROM {t}")  # noqa: S608


def _checked(sql: str) -> str:
    parsed = sqlglot.parse(sql, read="duckdb")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
        raise RuleError("a rule must compile to exactly one SELECT")
    if [e.alias_or_name for e in parsed[0].expressions] != ["checked", "failed"]:
        raise RuleError("a rule must return (checked, failed)")
    return sql


def run_rules(runner: CountRunner, rules: Sequence[Rule], as_of: date) -> list[Result]:
    out = []
    for r in rules:
        try:
            checked, failed = runner.check_counts(compile_rule(r, as_of))
            out.append(Result(r, checked, failed))
        except Exception as e:  # noqa: BLE001 - one broken rule must not hide the others; recorded as failed
            out.append(Result(r, 0, 0, error=f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"))
    return out


def run_and_record(settings: Any, as_of: date | None = None, record: bool = True) -> dict[str, Any]:
    """The dq_daily job body: run every rule as the pipeline and record counts through kb.record_dq_run."""
    from psycopg.types.json import Jsonb

    from kb.adapters import build
    from kb.adapters.local.registry import PostgresRegistry

    engine = build(settings, "sql_engine")
    rules, sha = load_rules({t: list(tp.columns) for t, tp in engine.policy.catalog.items()})
    as_of = as_of or datetime.now(UTC).date()
    started = datetime.now(UTC)
    results = run_rules(engine, rules, as_of)
    run = {
        "run_id": str(uuid.uuid4()),
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "rules_sha256": sha,
    }
    if record:
        reg = PostgresRegistry(settings.processing["registry_dsn"])  # pipeline role (kb_pipe)
        with reg.transaction("svc:dq") as tx:
            tx.execute("SELECT kb.record_dq_run(%s, %s)", (Jsonb(run), Jsonb([r.row() for r in results])))
    failed = [r.rule.id for r in results if not r.passed]
    return {"run": run, "passed": len(results) - len(failed), "failed": failed}
