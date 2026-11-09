"""Metric governance (Phase 9c, D-067): wrappers over the migration-012 functions, plus the seed metrics.

Definitions are validated against the mart catalogue first (the derived `columns` list is what the database checks
for masks). `python -m kb.semantic.governance seed` publishes the seed metrics through the normal flow
(data.maker drafts and submits, data.checker approves). Idempotent.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.types.json import Jsonb

from kb.semantic.model import validate


def _one(tx: Any, sql: str, params: Sequence[Any]) -> Any:
    row = tx.execute(sql, params).fetchone()
    return next(iter(row.values())) if isinstance(row, Mapping) else row[0]


def draft(tx: Any, metric_id: str, definition: Mapping[str, Any], description: str, columns: Sequence[str]) -> str:
    clean = validate(definition, columns)
    return str(_one(tx, "SELECT kb.draft_metric(%s, %s, %s)", (metric_id, Jsonb(clean), description)))


def submit(tx: Any, revision_id: str, comment: str | None = None) -> str:
    return str(_one(tx, "SELECT kb.submit_metric(%s, %s)", (revision_id, comment)))


def decide(tx: Any, request_id: str, approve: bool, reason: str | None = None) -> None:
    tx.execute("SELECT kb.decide_metric_request(%s, %s, %s)", (request_id, approve, reason))


def retire(tx: Any, metric_id: str, reason: str) -> str:
    return str(_one(tx, "SELECT kb.retire_metric(%s, %s)", (metric_id, reason)))


SEED: dict[str, tuple[dict[str, Any], str]] = {
    "npl_ratio": (
        {
            "label": "NPL ratio",
            "synonyms": ["non-performing loan ratio", "non performing loan ratio", "NPL rate"],
            "concept": "",
            "table": "mart_credit.loan",
            "measure": {
                "agg": "ratio",
                "scale": 100,
                "numerator": {"agg": "sum", "column": "outstanding", "where": "status = 'ARREARS_90'"},
                "denominator": {"agg": "sum", "column": "outstanding", "where": "status <> 'CLOSED'"},
            },
            "dimensions": ["bu", "product"],
            "unit": "%",
        },
        "Outstanding balance of loans 90+ days in arrears as a percentage of the outstanding balance of open loans.",
    ),
    "average_ltv": (
        {
            "label": "average LTV",
            "synonyms": ["average loan-to-value", "average loan to value", "mean LTV", "average loan-to-value ratio"],
            "concept": "",
            "table": "mart_credit.collateral",
            "measure": {"agg": "avg", "column": "ltv_pct"},
            "dimensions": ["bu", "collateral_type"],
            "unit": "%",
        },
        "Mean loan-to-value ratio of pledged collateral, in percent.",
    ),
    "aum": (
        {
            "label": "AUM",
            "synonyms": ["assets under management", "total assets under management"],
            "concept": "",
            "table": "mart_wealth.portfolio",
            "measure": {"agg": "sum", "column": "aum"},
            "dimensions": ["risk_profile", "mandate"],
            "unit": "EXB$",
        },
        "Total assets under management across wealth portfolios.",
    ),
    "loan_count": (
        {
            "label": "loan count",
            "synonyms": ["number of loans", "count of loans"],
            "concept": "",
            "table": "mart_credit.loan",
            "measure": {"agg": "count", "column": "loan_id"},
            "dimensions": ["product", "status", "bu"],
            "unit": "count",
        },
        "Number of loan facilities.",
    ),
}


def seed(registry: Any, catalog: Mapping[str, Sequence[str]], log: Any = print) -> list[str]:
    done = []
    for mid, (defn, desc) in SEED.items():
        with registry.transaction("data.maker") as tx:
            if tx.execute("SELECT 1 FROM kb.metric WHERE metric_id = %s", (mid,)).fetchone():
                log(f"  {mid}: exists, skipped")
                continue
            req = submit(tx, draft(tx, mid, defn, desc, catalog[defn["table"]]), "seed metric (user decision Q3)")
        with registry.transaction("data.checker") as tx:
            decide(tx, req, True, "seed metric approved")
        log(f"  {mid}: published (request {req[:8]})")
        done.append(mid)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.semantic.governance")
    ap.add_argument("command", choices=["seed"])
    ap.parse_args(argv)
    from kb.adapters import build
    from kb.config import load_settings

    settings = load_settings(strict=True)
    eng = build(settings, "sql_engine")
    seed(build(settings, "registry"), {t: tp.columns for t, tp in eng._policy.catalog.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
