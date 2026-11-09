"""D-066: product specs are validated, maker-written row filters are sanitised, and merging is exact."""

from __future__ import annotations

import pytest
import yaml

from kb.config import REPO_ROOT
from kb.sql.policy_store import SpecError, merge, safe_filter, specs_from_yaml, validate_spec

COLS = ["bu", "status", "principal", "email", "opened"]


@pytest.mark.parametrize(
    ("text", "out"),
    [
        ("bu = 'retail'", "bu = 'retail'"),
        ("bu IN ('retail','corporate') AND status <> 'CLOSED'", "bu IN ('retail', 'corporate') AND status <> 'CLOSED'"),
        ("principal BETWEEN 0 AND 1000", "principal BETWEEN 0 AND 1000"),
        ("NOT (status IS NULL)", "NOT (status IS NULL)"),
        ("bu = 'it''s'", "bu = 'it''s'"),
        ("TRUE", "TRUE"),  # all rows: legal, explicit, needs a checker like any widening
    ],
)
def test_safe_filters_are_reserialised(text, out):
    assert safe_filter(text, COLS) == out


@pytest.mark.parametrize(
    "text",
    [
        "bu = 'retail') OR (1=1",
        "bu IN (SELECT bu FROM mart_cust.customer)",
        "EXISTS (SELECT 1 FROM mart_cust.customer)",
        "lower(bu) = 'retail'",
        "CAST(bu AS INT) = 1",
        "x.bu = 'retail'",
        "secret = 1",
        "bu = 'retail'; DROP TABLE t",
        "1=1 UNION SELECT 1",
        "bu = 'a' -- comment",
        "bu = 'a' /* c */",
        "bu",
        "",
        "x" * 501,
        "current_user = 'a'",
        "bu = getvariable('x')",
    ],
)
def test_unsafe_filters_are_refused(text):
    with pytest.raises(SpecError):
        safe_filter(text, COLS)


def test_spec_validation_checks_columns_groups_and_masks():
    ok = validate_spec("t", {"grants": ["GRP_AA"], "row_filters": [{"groups": ["GRP_AA"], "filter": "bu='x'"}],
                             "column_masks": {"email": {"mask": "hash"}}}, COLS)  # fmt: skip
    assert ok["row_filters"][0]["filter"] == "bu = 'x'" and ok["column_masks"]["email"]["unless_groups"] == []
    for bad in (
        {"grants": ["nope"], "row_filters": [], "column_masks": {}},
        {"grants": ["GRP_AA"], "row_filters": [], "column_masks": {"ghost": {"mask": "redact"}}},
        {"grants": ["GRP_AA"], "row_filters": [], "column_masks": {"email": {"mask": "rot13"}}},
        {"grants": ["GRP_AA"], "row_filters": [{"groups": [], "filter": "bu='x'"}], "column_masks": {}},
        {"grants": ["GRP_AA"], "row_filters": [], "column_masks": {}, "sql": "x"},
    ):
        with pytest.raises(SpecError):
            validate_spec("t", bad, COLS)


def test_yaml_round_trips_through_product_specs():
    pol = yaml.safe_load((REPO_ROOT / "config" / "policies.yaml").read_text(encoding="utf-8"))
    tables = sorted({t for ts in pol["table_grants"].values() for t in ts})
    back = merge(specs_from_yaml(pol, tables))
    assert {g: sorted(t) for g, t in back["table_grants"].items()} == {
        g: sorted(t) for g, t in pol["table_grants"].items()
    }
    assert back["row_filters"] == pol["row_filters"]
    assert back["column_masks"] == {k: {"mask": v["mask"], "unless_groups": v.get("unless_groups", [])}
                                    for k, v in pol["column_masks"].items()}  # fmt: skip
