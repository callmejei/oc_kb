"""D-067: metric definitions are validated and compiled in code; matching is exact and conservative."""

from __future__ import annotations

import duckdb
import pytest

from kb.semantic.model import Metric, MetricError, compile_metric, match, validate

COLS = ["loan_id", "bu", "product", "outstanding", "status", "email"]
NPL = {
    "label": "NPL ratio",
    "synonyms": ["non-performing loan ratio"],
    "table": "mart_x.loan",
    "measure": {
        "agg": "ratio",
        "scale": 100,
        "numerator": {"agg": "sum", "column": "outstanding", "where": "status='ARREARS_90'"},
        "denominator": {"agg": "sum", "column": "outstanding", "where": "status<>'CLOSED'"},
    },
    "dimensions": ["bu", "product"],
    "unit": "%",
}
COUNT = {
    "label": "loan count",
    "synonyms": ["number of loans"],
    "table": "mart_x.loan",
    "measure": {"agg": "count", "column": "loan_id"},
    "dimensions": ["product"],
    "unit": "count",
}
METRICS = [Metric("npl_ratio", 1, validate(NPL, COLS), ""), Metric("loan_count", 1, validate(COUNT, COLS), "")]
RATIO_7 = {
    "agg": "ratio",
    "scale": 7,
    "numerator": {"agg": "sum", "column": "outstanding"},
    "denominator": {"agg": "sum", "column": "outstanding"},
}


def test_validation_derives_the_columns_used():
    assert validate(NPL, COLS)["columns"] == ["bu", "outstanding", "product", "status"]


@pytest.mark.parametrize(
    "patch",
    [
        {"measure": {"agg": "sum", "column": "nope"}},
        {"measure": {"agg": "median", "column": "outstanding"}},
        {"measure": {"agg": "sum", "column": "outstanding", "where": "status IN (SELECT 1)"}},
        {"measure": {"agg": "sum", "column": "outstanding", "where": "lower(status) = 'x'"}},
        {"measure": RATIO_7},
        {"dimensions": ["nope"]},
        {"unit": "parsecs"},
        {"label": "x"},
    ],
)
def test_invalid_definitions_are_refused(patch):
    with pytest.raises(MetricError):
        validate({**NPL, **patch}, COLS)


def test_compiled_sql_computes_the_right_value():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA mart_x")
    con.execute(
        "CREATE TABLE mart_x.loan (loan_id INT, bu TEXT, product TEXT, outstanding DOUBLE, status TEXT, email TEXT)"
    )
    con.execute(
        "INSERT INTO mart_x.loan VALUES (1,'r','A',100,'ARREARS_90',''), (2,'r','A',300,'CURRENT',''), "
        "(3,'r','B',600,'CLOSED',''), (4,'r','B',100,'CURRENT','')"
    )
    d = validate(NPL, COLS)
    assert con.execute(compile_metric(d)).fetchall() == [(20.0,)]  # 100 / (100 + 300 + 100)
    # status is not a dimension of NPL: it is ignored, never injected into the SQL
    assert con.execute(compile_metric(d, ["product", "status"])).fetchall() == [("A", 25.0), ("B", 0.0)]


@pytest.mark.parametrize(
    ("q", "want"),
    [
        ("What is our NPL ratio?", ("npl_ratio", ())),
        ("what is the non-performing loan ratio by product", ("npl_ratio", ("product",))),
        ("NPL ratio per bu", ("npl_ratio", ("bu",))),
        ("What is the number of loans?", ("loan_count", ())),
        ("loan count by product", ("loan_count", ("product",))),
        ("NPL ratio for home loans", None),  # narrower than the metric: text-to-SQL
        ("What was the NPL ratio in 2025?", None),
        ("number of loans by status", None),  # not a dimension of this metric
        ("Is the NPL ratio within appetite and what does the policy say?", None),
        ("How many customers are there?", None),
    ],
)
def test_matching_is_exact_and_conservative(q, want):
    m = match(q, METRICS)
    assert (None if m is None else (m.metric.metric_id, m.group_by)) == want
