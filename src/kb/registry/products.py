"""Data products (Phase 9b, D-066): thin wrappers over the migration-011 functions, plus the first seed.

Every call runs as the acting user (kb.actor_id) on the app role; the database enforces maker/checker, locking,
four-eyes and the single-actor restrictions. Specs are validated here first (columns, row-filter sanitising) so a
maker gets a precise error, and again by the policy engine on every load.

`python -m kb.registry.products seed` onboards every mart table from config/policies.yaml through the normal flow
(data.maker drafts and submits, data.checker approves). Idempotent: products that already exist are skipped.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import yaml
from psycopg.types.json import Jsonb

from kb.config import REPO_ROOT
from kb.sql.policy_store import validate_spec

CLASSIFICATIONS = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")
RESTRICTIONS = ("REVOKE_GRANT", "ADD_MASK", "SUSPEND")


def _one(tx: Any, sql: str, params: Sequence[Any]) -> Any:
    row = tx.execute(sql, params).fetchone()
    return next(iter(row.values())) if isinstance(row, Mapping) else row[0]


def draft(
    tx: Any,
    product_id: str,
    columns: Sequence[str],
    *,
    owner: str,
    description: str,
    classification: str,
    refresh: str,
    spec: Mapping[str, Any],
) -> str:
    clean = validate_spec(product_id, spec, columns)
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"classification must be one of {', '.join(CLASSIFICATIONS)}")
    return str(
        _one(
            tx,
            "SELECT kb.draft_product(%s, %s, %s, %s, %s, %s)",
            (product_id, owner, description, classification, refresh, Jsonb(clean)),
        )
    )


def submit(tx: Any, revision_id: str, comment: str | None = None) -> str:
    return str(_one(tx, "SELECT kb.submit_product(%s, %s)", (revision_id, comment)))


def decide(tx: Any, request_id: str, approve: bool, reason: str | None = None) -> None:
    tx.execute("SELECT kb.decide_product_request(%s, %s, %s)", (request_id, approve, reason))


def withdraw(tx: Any, request_id: str, reason: str | None = None) -> None:
    tx.execute("SELECT kb.withdraw_product_request(%s, %s)", (request_id, reason))


def restrict(
    tx: Any, product_id: str, action: str, reason: str, *, group: str | None = None, column: str | None = None,
    mask: str = "redact",
) -> str:  # fmt: skip
    if action not in RESTRICTIONS:
        raise ValueError(f"restriction must be one of {', '.join(RESTRICTIONS)}")
    sql = "SELECT kb.restrict_product(%s, %s, %s, %s, %s, %s)"
    return str(_one(tx, sql, (product_id, action, reason, group, column, mask)))


def request_reactivation(tx: Any, product_id: str, comment: str) -> str:
    return str(_one(tx, "SELECT kb.request_product_reactivation(%s, %s)", (product_id, comment)))


# ------------------------------------------------------------------------------------------------ seed
def seed(registry: Any, catalog: Mapping[str, Sequence[str]], maker: str = "data.maker", checker: str = "data.checker",
         log: Any = print) -> list[str]:  # fmt: skip
    from kb.sql.policy_store import specs_from_yaml

    pol = yaml.safe_load((REPO_ROOT / "config" / "policies.yaml").read_text(encoding="utf-8"))
    marts = yaml.safe_load((REPO_ROOT / "config" / "marts.yaml").read_text(encoding="utf-8"))["tables"]
    specs = specs_from_yaml(pol, sorted(catalog))
    done = []
    for table, spec in specs.items():
        with registry.transaction(maker) as tx:
            if tx.execute("SELECT 1 FROM kb.data_product WHERE product_id = %s", (table,)).fetchone():
                log(f"  {table}: exists, skipped")
                continue
            rev = draft(
                tx,
                table,
                catalog[table],
                owner="Group Data Office (synthetic)",
                description=marts.get(table, {}).get("description", table),
                classification="CONFIDENTIAL" if table == "mart_cust.customer" else "INTERNAL",
                refresh="daily 02:00 SGT",
                spec=spec,
            )
            req = submit(tx, rev, "first seed from config/policies.yaml")
        with registry.transaction(checker) as tx:
            decide(tx, req, True, "first seed: policies carried over unchanged from config/policies.yaml")
        log(f"  {table}: onboarded (request {req[:8]})")
        done.append(table)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.registry.products")
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
