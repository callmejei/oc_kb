"""Phase 9a: data-quality rules are structured, validated against the catalogue, compiled to one aggregate SELECT,
and the pipeline runner returns only two counts. Runs on a throwaway DuckDB with deliberately broken rows."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from kb.quality.rules import Rule, RuleError, compile_rule, load_rules, run_rules, validate

CAT = {
    "mart_x.loan": ["loan_id", "customer_id", "status", "rate", "valued"],
    "mart_x.cust": ["customer_id"],
}
AS_OF = date(2026, 11, 9)


@pytest.fixture()
def con():  # type: ignore[no-untyped-def]
    c = duckdb.connect()
    c.execute("CREATE SCHEMA mart_x")
    c.execute("CREATE TABLE mart_x.cust (customer_id VARCHAR)")
    c.execute("INSERT INTO mart_x.cust VALUES ('C1'), ('C2')")
    c.execute(
        "CREATE TABLE mart_x.loan (loan_id VARCHAR, customer_id VARCHAR, status VARCHAR, rate DOUBLE, valued DATE)"
    )
    c.execute(
        """INSERT INTO mart_x.loan VALUES
           ('L1', 'C1', 'CURRENT', 3.5, DATE '2026-06-01'),
           ('L1', 'C9', 'BOGUS',   -1,  DATE '2023-01-01'),   -- duplicate id, orphan, bad status, bad rate, stale
           ('L3', NULL, 'CLOSED',  NULL, NULL),
           ('L4', 'C2', 'O''Brien', 45, DATE '2026-10-01')""")  # fmt: skip
    yield c
    c.close()


class Runner:
    def __init__(self, con):  # type: ignore[no-untyped-def]
        self.con = con

    def check_counts(self, sql: str) -> tuple[int, int]:
        (a, b), *rest = self.con.execute(sql).fetchall()
        assert not rest
        return int(a), int(b)


@pytest.mark.parametrize(
    ("rule", "failed"),
    [
        ({"id": "a_unique", "table": "mart_x.loan", "column": "loan_id", "kind": "unique"}, 1),
        ({"id": "a_ref", "table": "mart_x.loan", "column": "customer_id", "kind": "reference",
          "ref_table": "mart_x.cust", "ref_column": "customer_id"}, 1),  # NULL is not an orphan
        ({"id": "a_null", "table": "mart_x.loan", "column": "rate", "kind": "not_null"}, 1),
        ({"id": "a_range", "table": "mart_x.loan", "column": "rate", "kind": "range", "min": 0, "max": 30}, 2),
        ({"id": "a_values", "table": "mart_x.loan", "column": "status", "kind": "allowed_values",
          "values": ["CURRENT", "CLOSED", "O'Brien"]}, 1),  # a quote in a value is a literal, not SQL
        ({"id": "a_age", "table": "mart_x.loan", "column": "valued", "kind": "max_age", "max_age_days": 730}, 2),
    ],
)  # fmt: skip
def test_each_kind_counts_the_broken_rows(con, rule, failed):
    (res,) = run_rules(Runner(con), [validate(rule, CAT)], AS_OF)
    assert (res.checked, res.failed, res.error) == (4, failed, None)
    assert not res.passed


def test_tolerance_turns_a_small_failure_into_a_pass(con):
    r = validate({"id": "a_age", "table": "mart_x.loan", "column": "valued", "kind": "max_age",
                  "max_age_days": 730, "tolerance": 0.5}, CAT)  # fmt: skip
    (res,) = run_rules(Runner(con), [r], AS_OF)
    assert res.failed == 2 and res.passed


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "x_col", "table": "mart_x.loan", "column": "rate; DROP TABLE mart_x.loan", "kind": "not_null"},
        {"id": "x_tab", "table": "mart_x.loan UNION SELECT 1", "column": "rate", "kind": "not_null"},
        {"id": "x_unknown", "table": "mart_x.nope", "column": "rate", "kind": "not_null"},
        {"id": "x_kind", "table": "mart_x.loan", "column": "rate", "kind": "sql"},
        {"id": "x_range", "table": "mart_x.loan", "column": "rate", "kind": "range"},
        {"id": "x_minmax", "table": "mart_x.loan", "column": "rate", "kind": "range", "min": 5, "max": 1},
        {"id": "x_vals", "table": "mart_x.loan", "column": "status", "kind": "allowed_values", "values": [{"a": 1}]},
        {"id": "x_ref", "table": "mart_x.loan", "column": "customer_id", "kind": "reference",
         "ref_table": "mart_x.cust", "ref_column": "customer_id) OR (1=1"},
        {"id": "x_tol", "table": "mart_x.loan", "column": "rate", "kind": "not_null", "tolerance": 2},
        {"id": "Bad Id", "table": "mart_x.loan", "column": "rate", "kind": "not_null"},
        {"id": "x_range_txt", "table": "mart_x.loan", "column": "rate", "kind": "range", "min": "0 OR 1=1"},
    ],
)  # fmt: skip
def test_invalid_rules_are_rejected(bad):
    with pytest.raises((RuleError, ValueError)):
        validate(bad, CAT)


def test_a_broken_rule_is_recorded_as_failed_not_hidden(con):
    good = validate({"id": "a_null", "table": "mart_x.loan", "column": "rate", "kind": "not_null"}, CAT)
    ghost = Rule("ghost", "mart_x.gone", "rate", "not_null")  # table dropped after validation
    res = run_rules(Runner(con), [ghost, good], AS_OF)
    assert res[0].error and not res[0].passed and res[1].error is None


def test_compiled_sql_is_one_select_of_two_counts():
    r = validate({"id": "a_values", "table": "mart_x.loan", "column": "status", "kind": "allowed_values",
                  "values": ["a'b"]}, CAT)  # fmt: skip
    sql = compile_rule(r, AS_OF)
    assert sql.startswith("SELECT count(*) AS checked") and "'a''b'" in sql and ";" not in sql


def test_the_shipped_rules_load_against_the_real_catalogue():
    from kb.adapters import build
    from kb.config import load_settings

    eng = build(load_settings(), "sql_engine")
    rules, sha = load_rules({t: list(tp.columns) for t, tp in eng.policy.catalog.items()})
    assert len(rules) >= 10 and len(sha) == 64
    assert {r.table for r in rules} == set(eng.policy.catalog)  # every mart table has at least one rule


def test_pipeline_runner_returns_only_two_counts():
    from kb.adapters import build
    from kb.config import load_settings
    from kb.sql.policy import PolicyError

    eng = build(load_settings(), "sql_engine")
    assert eng.check_counts("SELECT count(*), 0 FROM mart_credit.loan")[0] > 0
    for sql in (
        "SELECT customer_id, 1 FROM mart_cust.customer",  # values, many rows
        "SELECT 'x', 1",  # not counts
        "SELECT 1, 2; SELECT 3, 4",  # two statements
        "DELETE FROM mart_credit.loan",
        "SELECT count(*), count(*), 1 FROM mart_credit.loan",  # three columns
    ):
        with pytest.raises(PolicyError):
            eng.check_counts(sql)
