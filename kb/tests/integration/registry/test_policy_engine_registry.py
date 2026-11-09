"""D-066 end to end: the SQL engine reads approved data products from the registry (kb_test). A restriction applies
to the very next query without a restart; every decision carries the policy version; an unreadable registry fails
closed; a spec that fails validation at load denies its table."""

from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from kb.adapters.local.identity import YamlIdentityProvider
from kb.adapters.local.sql_engine import DuckDbPolicySqlEngine
from kb.sql.policy import PolicyError

IDP = YamlIdentityProvider("config/users.yaml")
LOAN = {
    "grants": ["GRP_DATA_CREDIT_RETAIL", "GRP_DATA_CREDIT_CORPORATE"],
    "row_filters": [
        {"groups": ["GRP_DATA_CREDIT_RETAIL"], "filter": "bu = 'retail'"},
        {"groups": ["GRP_DATA_CREDIT_CORPORATE"], "filter": "bu = 'corporate'"},
    ],
    "column_masks": {},
}


def one(c, sql, *params):  # type: ignore[no-untyped-def]
    return next(iter(c.execute(sql, params).fetchone().values()))


@pytest.fixture(scope="module")
def engine(api_dsn, regdb):  # type: ignore[no-untyped-def]
    import psycopg
    from psycopg.rows import dict_row

    def as_actor(actor: str):  # type: ignore[no-untyped-def]
        c = psycopg.connect(api_dsn, autocommit=True, row_factory=dict_row)
        c.execute("SELECT set_config('kb.actor_id', %s, false)", (actor,))
        return c

    with as_actor("data.maker") as c:
        if not c.execute("SELECT 1 FROM kb.data_product WHERE product_id = 'mart_credit.loan'").fetchone():
            rev = one(c, "SELECT kb.draft_product('mart_credit.loan', 'o', 'd', 'INTERNAL', 'r', %s)", Jsonb(LOAN))
            req = one(c, "SELECT kb.submit_product(%s, NULL)", rev)
            with as_actor("data.checker") as k:
                k.execute("SELECT kb.decide_product_request(%s, true, 'ok')", (req,))
    eng = DuckDbPolicySqlEngine("data/marts/marts.duckdb", policy_source="registry", policy_dsn=api_dsn)
    eng.as_actor = as_actor  # type: ignore[attr-defined]
    return eng


def test_only_approved_products_are_queryable(engine):
    ret = IDP.resolve("ret.user1")
    assert engine.policy.granted_tables(ret) == ["mart_credit.loan"]  # the other marts have no product in kb_test
    with pytest.raises(PolicyError, match="not permitted"):
        engine.execute("SELECT count(*) FROM mart_cust.customer", ret, row_limit=5)


def test_a_revocation_applies_to_the_next_query_and_is_versioned(engine):
    corp = IDP.resolve("corp.user1")
    r1 = engine.execute("SELECT DISTINCT bu FROM mart_credit.loan", corp, row_limit=5)
    v1 = engine.last_decision.policy_version
    assert r1.rows == [("corporate",)] and v1 is not None
    with engine.as_actor("data.checker") as c:
        c.execute(
            "SELECT kb.restrict_product('mart_credit.loan', 'REVOKE_GRANT', 'test', %s, NULL, 'redact')",
            ("GRP_DATA_CREDIT_CORPORATE",),
        )
    with pytest.raises(PolicyError, match="not permitted"):  # same engine object, no restart
        engine.execute("SELECT DISTINCT bu FROM mart_credit.loan", corp, row_limit=5)
    ret = IDP.resolve("ret.user1")
    engine.execute("SELECT DISTINCT bu FROM mart_credit.loan", ret, row_limit=5)
    assert engine.last_decision.policy_version == v1 + 1


def test_an_unreadable_registry_fails_closed():
    eng = DuckDbPolicySqlEngine(
        "data/marts/marts.duckdb",
        policy_source="registry",
        policy_dsn="postgresql://kb_api:wrong@127.0.0.1:5433/kb_test",
    )
    with pytest.raises(PolicyError, match="cannot be read"):
        eng.execute("SELECT 1 FROM mart_credit.loan", IDP.resolve("ret.user1"), row_limit=1)
    with pytest.raises(PolicyError):
        eng.permitted_schema(IDP.resolve("ret.user1"))
