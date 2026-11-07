"""Phase 4 acceptance at engine level: masks, row filters, grants, and refusal of DML/DDL and escapes."""

from __future__ import annotations

import duckdb
import pytest

from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.sql.policy import PolicyError

IDP = YamlIdentityProvider("config/users.yaml")
RAW_PII = ("Test Customer", "@example.invalid", "+00 0000", "FAKE-ID-")


@pytest.fixture(scope="module")
def engine(settings):
    return build(settings, "sql_engine")


@pytest.fixture(scope="module")
def truth(data_dir):
    con = duckdb.connect(str(data_dir / "marts" / "marts.duckdb"), read_only=True)
    yield lambda q: con.execute(q).fetchall()
    con.close()


def run(engine, user, sql, limit=200):  # type: ignore[no-untyped-def]
    return engine.execute(sql, IDP.resolve(user), row_limit=limit)


def flat(result) -> str:  # type: ignore[no-untyped-def]
    return " ".join(str(v) for row in result.rows for v in row)


# ------------------------------------------------------------------------------------------------ masks
@pytest.mark.parametrize("user", ["ret.user1", "corp.user1", "wlth.user1"])
def test_masked_columns_never_return_raw_values(engine, user):
    r = run(engine, user, "SELECT full_name, email, phone, national_id, date_of_birth FROM mart_cust.customer")
    text = flat(r)
    assert r.rows and not any(p in text for p in RAW_PII)
    assert {row[0] for row in r.rows} == {"REDACTED"} and all(row[4].month == 1 and row[4].day == 1 for row in r.rows)
    assert all(len(row[3]) == 16 for row in r.rows)  # hashed national id


def test_unmasked_group_sees_real_values(engine):
    r = run(engine, "data.steward", "SELECT full_name, email FROM mart_cust.customer LIMIT 3")
    assert all(row[0].startswith("Test Customer R-") and row[1].endswith("@example.invalid") for row in r.rows)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM mart_cust.customer WHERE full_name LIKE 'Test Customer R-0001%'",
        "SELECT count(*) FROM mart_cust.customer WHERE email = 'cust.r00001@example.invalid'",
        "SELECT count(*) FROM mart_cust.customer WHERE substr(national_id, 1, 8) = 'FAKE-ID-'",
    ],
)
def test_filtering_on_masked_columns_reveals_nothing(engine, sql):
    assert run(engine, "ret.user1", sql).rows == [(0,)]


def test_client_ref_masked_even_for_wealth(engine):
    r = run(engine, "wlth.user1", "SELECT DISTINCT client_ref FROM mart_wealth.portfolio LIMIT 5")
    assert not any(str(v[0]).startswith("CLIENT-REF") for v in r.rows)


# ------------------------------------------------------------------------------------------------ row filters
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT bu, count(*) FROM mart_credit.loan GROUP BY bu",
        "SELECT bu, count(*) FROM mart_credit.loan WHERE bu = 'corporate' OR 1 = 1 GROUP BY bu",
        "SELECT l.bu, count(*) FROM mart_credit.loan l JOIN mart_cust.customer c USING (customer_id) GROUP BY l.bu",
        "SELECT bu, count(*) FROM (SELECT bu FROM mart_credit.loan "
        "UNION ALL SELECT bu FROM mart_credit.collateral) GROUP BY bu",
        "WITH x AS (SELECT * FROM mart_credit.loan) SELECT bu, count(*) FROM x GROUP BY bu",
        "SELECT bu, count(*) FROM credit_loans_alias_trick GROUP BY bu".replace("credit_loans_alias_trick", "loan"),
    ],
)
def test_retail_user_only_ever_sees_retail_rows(engine, truth, sql):
    r = run(engine, "ret.user1", sql)
    assert {row[0] for row in r.rows} == {"retail"}


def test_counts_match_independent_ground_truth(engine, truth):
    for user, bu in (("ret.user1", "retail"), ("corp.user1", "corporate")):
        expect = truth(f"SELECT count(*) FROM mart_credit.loan WHERE bu = '{bu}' AND status = 'ARREARS_90'")[0][0]  # noqa: S608
        got = run(engine, user, "SELECT count(*) FROM mart_credit.loan WHERE status = 'ARREARS_90'").rows[0][0]
        assert got == expect


def test_grants_are_enforced(engine):
    with pytest.raises(PolicyError, match="not permitted.*mart_credit.loan") as e:
        run(engine, "wlth.user1", "SELECT count(*) FROM mart_credit.loan")
    assert e.value.code == "not_granted"
    for user in ("ret.user2", "grp.reader", "nobody"):
        with pytest.raises(PolicyError):
            run(engine, user, "SELECT count(*) FROM mart_cust.customer")
    assert engine.permitted_schema(IDP.resolve("nobody")) == {}


def test_row_limit_is_capped(engine):
    r = run(engine, "ret.user1", "SELECT loan_id FROM mart_credit.loan", limit=100000)
    assert len(r.rows) == engine.policy.cfg["max_rows"] and r.executed_sql.endswith("truncated")


def test_schema_context_shows_only_permitted_tables_and_marks_masks(engine):
    ctx = engine.schema_context(IDP.resolve("ret.user1"))
    assert "mart_credit.loan" in ctx and "mart_wealth.portfolio" not in ctx and "MASKED" in ctx
    assert "mart_credit" not in engine.schema_context(IDP.resolve("wlth.user1"))


# ------------------------------------------------------------------------------------------------ refusals
REFUSE = {
    "DELETE FROM mart_credit.loan": "not_select",
    "DROP TABLE mart_credit.loan": "not_select",
    "INSERT INTO mart_credit.loan SELECT * FROM mart_credit.loan": "not_select",
    "UPDATE mart_credit.loan SET principal = 0": "not_select",
    "CREATE TABLE x AS SELECT * FROM mart_credit.loan": "not_select",
    "SELECT 1; DROP TABLE mart_credit.loan": "multiple_statements",
    "SELECT count(*) FROM mart_credit.loan; SELECT 1": "multiple_statements",
    "ATTACH 'x.db' AS x": "not_select",
    "COPY mart_credit.loan TO 'out.csv'": "not_select",
    "INSTALL httpfs": "not_select",
    "LOAD httpfs": "not_select",
    "PRAGMA database_list": "not_select",
    "SET enable_external_access = true": "not_select",
    "SELECT * FROM read_csv('config/users.yaml')": "table_function",
    "SELECT * FROM read_parquet('C:/Windows/x.parquet')": "table_function",
    "SELECT * FROM glob('*')": "table_function",
    "SELECT getenv('KB_PG_ADMIN_PASSWORD')": "function_not_allowed",
    "SELECT * FROM duckdb_tables()": "table_function",
    "SELECT current_setting('memory_limit')": "function_not_allowed",
    "SELECT * FROM information_schema.tables": "unknown_table",
    "SELECT * FROM kb.audit_log": "unknown_table",
    "SELECT * FROM mart_credit.loan, mart_cust.customer": "cartesian_join",
    "SELECT * FROM mart_credit.loan CROSS JOIN mart_credit.collateral": "cartesian_join",
    "SELECT * FROM other.mart_credit.loan": "unknown_table",
    "WITH d AS (DELETE FROM mart_credit.loan RETURNING *) SELECT * FROM d": "parse_error|not_select",
    "this is not sql": "parse_error|not_select",
}


@pytest.mark.parametrize(("sql", "code"), list(REFUSE.items()))
def test_dml_ddl_and_escapes_are_refused(engine, sql, code):
    with pytest.raises(PolicyError) as e:
        run(engine, "data.steward", sql)
    assert e.value.code in code.split("|"), (sql, e.value.code, str(e.value))


def test_connection_is_hardened_even_if_the_parser_were_bypassed(engine):
    con = engine._con.cursor()
    try:
        for stmt in (
            "SELECT * FROM read_csv('config/users.yaml')",
            "SET enable_external_access = true",
            "INSTALL httpfs",
        ):
            with pytest.raises(duckdb.Error):
                con.execute(stmt)
        with pytest.raises(duckdb.Error):
            con.execute("DELETE FROM mart_credit.loan")  # read-only file
    finally:
        con.close()
