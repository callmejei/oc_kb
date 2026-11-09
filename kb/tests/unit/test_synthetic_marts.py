"""Marts exist, values are obviously fake, and policies.yaml is consistent with them and users.yaml."""

from __future__ import annotations

import duckdb
import pytest
import yaml

from kb.config import CONFIG_DIR

TABLES = ["mart_credit.loan", "mart_credit.collateral", "mart_cust.customer", "mart_wealth.portfolio"]


@pytest.fixture(scope="module")
def con(data_dir):
    c = duckdb.connect(str(data_dir / "marts" / "marts.duckdb"), read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def policies():
    return yaml.safe_load((CONFIG_DIR / "policies.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def groups():
    return set(yaml.safe_load((CONFIG_DIR / "users.yaml").read_text(encoding="utf-8"))["groups"])


@pytest.mark.parametrize("table", TABLES)
def test_tables_exist_and_have_rows(con, table):
    assert con.execute(f"select count(*) from {table}").fetchone()[0] > 100  # noqa: S608


def test_pii_shaped_columns_are_obviously_fake(con):
    bad = con.execute("""
        select count(*) from mart_cust.customer
        where full_name not like 'Test Customer %' or email not like '%@example.invalid'
           or phone not like '+00 %' or national_id not like 'FAKE-ID-%'
    """).fetchone()[0]
    assert bad == 0


def test_every_bu_present_for_row_filters(con):
    assert {r[0] for r in con.execute("select distinct bu from mart_cust.customer").fetchall()} == {
        "retail",
        "corporate",
        "wealth",
    }
    assert {r[0] for r in con.execute("select distinct bu from mart_credit.loan").fetchall()} == {"retail", "corporate"}


def test_referential_integrity(con):
    assert (
        con.execute("""select count(*) from mart_credit.loan l
                          left join mart_cust.customer c using (customer_id) where c.customer_id is null""").fetchone()[
            0
        ]
        == 0
    )
    assert (
        con.execute("""select count(*) from mart_credit.collateral k
                          left join mart_credit.loan l using (loan_id) where l.loan_id is null""").fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "select count(*) from mart_credit.collateral where bu <> "
            "(select bu from mart_credit.loan l where l.loan_id = collateral.loan_id)"
        ).fetchone()[0]
        == 0
    )


def test_policy_groups_exist_in_users_yaml(policies, groups):
    referenced = set(policies["table_grants"])
    for rules in policies["row_filters"].values():
        for r in rules:
            referenced |= set(r["groups"])
    for m in policies["column_masks"].values():
        referenced |= set(m["unless_groups"])
    assert referenced <= groups, referenced - groups


def test_policy_tables_and_columns_exist(con, policies):
    cols = {
        f"{s}.{t}.{c}"
        for s, t, c in con.execute(
            "select table_schema, table_name, column_name from information_schema.columns"
        ).fetchall()
    }
    tables = {c.rsplit(".", 1)[0] for c in cols}
    for ts in policies["table_grants"].values():
        assert set(ts) <= tables
    assert set(policies["row_filters"]) <= tables
    assert set(policies["column_masks"]) <= cols


def test_every_granted_table_has_a_row_filter(policies):
    granted = {t for ts in policies["table_grants"].values() for t in ts}
    assert granted <= set(policies["row_filters"]), "deny-by-default needs a filter for every granted table"
