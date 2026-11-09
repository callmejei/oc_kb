"""PolicyEngine: Ranger-style table grants, row filters and column masks, applied by rewriting validated SQL.

Pipeline for every query (CLAUDE.md Phase 4):
  parse (sqlglot, DuckDB dialect) -> validate (one SELECT; known mart tables; allowlisted functions only;
  no cartesian join on large tables; join cap) -> authorise (every table granted to one of the caller's
  groups) -> rewrite (each table becomes a subquery applying the caller's row filter, with masked columns
  replaced by their masked value) -> enforce a row limit.

Masking happens inside the per-table subquery, so predicates, joins and aggregates only ever see masked
values (like a Ranger-masked view): filtering on a masked column cannot reveal the raw value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from kb.config import REPO_ROOT
from kb.ports.types import Identity

DIALECT = "duckdb"

# Functions a generated query may call. Everything else is refused (table functions, file readers,
# system/introspection functions, getenv, extension loaders, ...). Names as sqlglot renders them.
ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "MEDIAN",
        "STDDEV",
        "STDDEV_SAMP",
        "STDDEV_POP",
        "VARIANCE",
        "COUNT_IF",
        "COUNTIF",
        "QUANTILE_CONT",
        "QUANTILE_DISC",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
        "APPROX_DISTINCT",
        "ARRAY_AGG",
        "STRING_AGG",
        "GROUP_CONCAT",
        "LISTAGG",
        "ANY_VALUE",
        "BOOL_AND",
        "BOOL_OR",
        "FIRST",
        "LAST",
        "MODE",
        # window
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "NTILE",
        "LAG",
        "LEAD",
        "PERCENT_RANK",
        "CUME_DIST",
        # numeric
        "ROUND",
        "ABS",
        "FLOOR",
        "CEIL",
        "CEILING",
        "POWER",
        "POW",
        "SQRT",
        "LN",
        "LOG",
        "EXP",
        "SIGN",
        "MOD",
        "GREATEST",
        "LEAST",
        "TRUNC",
        # conditional / null handling / casting
        "COALESCE",
        "NULLIF",
        "IF",
        "CASE",
        "IFF",
        "IIF",
        "CAST",
        "TRY_CAST",
        "IFNULL",
        "NVL",
        # strings
        "LOWER",
        "UPPER",
        "LENGTH",
        "SUBSTRING",
        "SUBSTR",
        "CONCAT",
        "TRIM",
        "LTRIM",
        "RTRIM",
        "REPLACE",
        "LEFT",
        "RIGHT",
        "STARTS_WITH",
        "ENDS_WITH",
        "CONTAINS",
        "STRPOS",
        "SPLIT_PART",
        "LPAD",
        "RPAD",
        # dates
        "DATE_TRUNC",
        "DATETRUNC",
        "TIMESTAMP_TRUNC",
        "EXTRACT",
        "DATE_PART",
        "YEAR",
        "MONTH",
        "DAY",
        "QUARTER",
        "WEEK",
        "DAYOFWEEK",
        "DAYOFYEAR",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "DATE_DIFF",
        "DATEDIFF",
        "DATE_ADD",
        "DATEADD",
        "DATE_SUB",
        "STRFTIME",
        "TIME_TO_STR",
        "MAKE_DATE",
        "DATE",
        "TS_OR_DS_ADD",
        "TS_OR_DS_TO_DATE",
        "STR_TO_DATE",
        "STRPTIME",
        "TO_DATE",
        "AGE",
        "INTERVAL",
    }
)
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Copy,
    exp.Set,
    exp.Pragma,
    exp.Use,
    exp.Into,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)


class PolicyError(ValueError):
    """The query is refused. ``code`` says why (for audit and for the text-to-SQL retry)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TablePolicy:
    name: str  # schema.table
    columns: tuple[str, ...]
    rows: int


@dataclass
class Decision:
    tables: list[str] = field(default_factory=list)
    row_filters: dict[str, str] = field(default_factory=dict)
    masked: dict[str, list[str]] = field(default_factory=dict)
    limit: int = 0


class PolicyEngine:
    def __init__(self, policies_path: str | Path, marts_path: str | Path, catalog: dict[str, TablePolicy]) -> None:
        pp, mp = Path(policies_path), Path(marts_path)
        pp = pp if pp.is_absolute() else REPO_ROOT / pp
        mp = mp if mp.is_absolute() else REPO_ROOT / mp
        pol = yaml.safe_load(pp.read_text(encoding="utf-8"))
        marts = yaml.safe_load(mp.read_text(encoding="utf-8"))
        self.grants: dict[str, list[str]] = pol["table_grants"]
        self.row_filters: dict[str, list[dict[str, Any]]] = pol["row_filters"]
        self.masks: dict[str, dict[str, Any]] = pol["column_masks"]
        self.cfg: dict[str, Any] = marts["engine"]
        self.descriptions: dict[str, Any] = marts["tables"]
        self.catalog = catalog
        self.short_names = self._short_names()

    def _short_names(self) -> dict[str, str]:
        seen: dict[str, list[str]] = {}
        for full in self.catalog:
            seen.setdefault(full.split(".", 1)[1], []).append(full)
        return {short: fulls[0] for short, fulls in seen.items() if len(fulls) == 1}

    # ---------------------------------------------------------------- what a user may see
    def granted_tables(self, ident: Identity) -> list[str]:
        return sorted({t for g in ident.groups for t in self.grants.get(g, [])} & set(self.catalog))

    def masked_columns(self, ident: Identity, table: str) -> dict[str, str]:
        out = {}
        for key, m in self.masks.items():
            t, col = key.rsplit(".", 1)
            if t == table and not (set(m.get("unless_groups", [])) & ident.groups):
                out[col] = m["mask"]
        return out

    def row_filter(self, ident: Identity, table: str) -> str:
        rules = [r["filter"] for r in self.row_filters.get(table, []) if set(r["groups"]) & ident.groups]
        return " OR ".join(f"({f})" for f in dict.fromkeys(rules)) if rules else "FALSE"  # deny by default

    # ---------------------------------------------------------------- the pipeline
    def prepare(self, sql: str, ident: Identity, row_limit: int) -> tuple[str, Decision]:
        tree = self._parse(sql)
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        tables = self._validate(tree, cte_names)
        decision = Decision(tables=sorted(set(tables.values())))
        granted = set(self.granted_tables(ident))
        for full in decision.tables:
            if full not in granted:
                raise PolicyError("not_granted", f"you are not permitted to query {full}")
        for node, full in tables.items():
            node.replace(self._policy_subquery(node, full, ident, decision))
        limit = max(1, min(int(row_limit), int(self.cfg["max_rows"])))
        decision.limit = limit
        # S608: built from the validated, re-serialised AST and an int, never from raw user text
        rewritten = f"SELECT * FROM ({tree.sql(dialect=DIALECT)}) AS q LIMIT {limit + 1}"  # noqa: S608
        return rewritten, decision

    def _parse(self, sql: str) -> exp.Expression:
        try:
            stmts = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except sqlglot.errors.ParseError as e:
            raise PolicyError("parse_error", f"could not parse SQL: {str(e).splitlines()[0]}") from None
        if len(stmts) != 1:
            raise PolicyError("multiple_statements", "exactly one SQL statement is allowed")
        tree = stmts[0]
        if not isinstance(tree, (exp.Select, exp.SetOperation)):
            raise PolicyError("not_select", f"only SELECT queries are allowed (got {tree.key.upper()})")
        for bad in FORBIDDEN_NODES:
            if tree.find(bad):
                raise PolicyError("not_select", f"{bad.__name__.upper()} is not allowed")
        return tree

    def _validate(self, tree: exp.Expression, cte_names: set[str]) -> dict[exp.Table, str]:
        tables: dict[exp.Table, str] = {}
        for t in tree.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                raise PolicyError("table_function", "table functions are not allowed")
            name, db = t.name.lower(), (t.db or "").lower()
            if not db and name in cte_names:
                continue
            full = f"{db}.{name}" if db else self.short_names.get(name, name)
            if t.catalog or full not in self.catalog:
                raise PolicyError("unknown_table", f"unknown table {t.sql(dialect=DIALECT)}")
            tables[t] = full
        for f in tree.find_all(exp.Func):
            if isinstance(f, exp.Connector):  # AND / OR are operators, not functions
                continue
            fname = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).upper()
            if fname not in ALLOWED_FUNCTIONS:
                raise PolicyError("function_not_allowed", f"function {fname} is not allowed")
        joins = list(tree.find_all(exp.Join))
        if len(joins) > int(self.cfg["max_joins"]):
            raise PolicyError("too_many_joins", f"at most {self.cfg['max_joins']} joins are allowed")
        large = {n for n, p in self.catalog.items() if p.rows >= int(self.cfg["large_table_rows"])}
        for j in joins:
            cartesian = (j.args.get("kind") or "").upper() == "CROSS" or not (j.args.get("on") or j.args.get("using"))
            if cartesian and (set(tables.values()) & large):
                raise PolicyError("cartesian_join", "cross/cartesian joins on large tables are not allowed")
        return tables

    def _policy_subquery(self, node: exp.Table, full: str, ident: Identity, d: Decision) -> exp.Expression:
        masks = self.masked_columns(ident, full)
        filt = self.row_filter(ident, full)
        d.row_filters[full] = filt
        d.masked[full] = sorted(masks)
        cols = []
        for c in self.catalog[full].columns:
            m = masks.get(c)
            if m is None:
                cols.append(f'"{c}"')
            elif m == "redact":
                cols.append(f"'REDACTED' AS \"{c}\"")
            elif m == "hash":
                cols.append(f'substr(sha256(CAST("{c}" AS VARCHAR)), 1, 16) AS "{c}"')
            elif m == "year_only":
                cols.append(f'make_date(year("{c}"), 1, 1) AS "{c}"')
            else:  # null and anything unknown: fail closed
                cols.append(f'NULL AS "{c}"')
        schema, table = full.split(".")
        # S608: catalog columns/tables and policies.yaml filters only, never user text
        sql = f'SELECT {", ".join(cols)} FROM "{schema}"."{table}" WHERE {filt}'  # noqa: S608
        sub = sqlglot.parse_one(sql, read=DIALECT)
        return exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier(node.alias_or_name)))
