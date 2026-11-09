"""Phase 9a quality API: data-quality rules only for granted tables (ops: all), knowledge health only for readable
BUs (ops: all), counts only. Every user is checked against the policy engine and the identity provider."""

from __future__ import annotations

import uuid

import pytest
from psycopg.types.json import Jsonb

from kb.adapters.local.identity import YamlIdentityProvider
from kb.gateway.routes_ops import OPS_GROUP

IDP = YamlIdentityProvider("config/users.yaml")
USERS = [u.user_id for u in IDP.all_users()]
TABLES = ["mart_credit.loan", "mart_credit.collateral", "mart_cust.customer", "mart_wealth.portfolio"]


@pytest.fixture(scope="module")
def dq_run(pipe_dsn):  # type: ignore[no-untyped-def]
    import psycopg
    from psycopg.rows import dict_row

    run = {"run_id": str(uuid.uuid4()), "started_at": "2099-01-01T00:00:00+00:00",
           "finished_at": "2099-01-01T00:00:01+00:00", "as_of": "2099-01-01", "rules_sha256": "c" * 64}  # fmt: skip
    results = [
        {"rule_id": f"r_{i}", "table_name": t, "rule_kind": "not_null", "column_name": "bu",
         "checked": 10, "failed": i % 2, "tolerance": 0, "passed": i % 2 == 0}
        for i, t in enumerate(TABLES)
    ]  # fmt: skip
    with psycopg.connect(pipe_dsn, row_factory=dict_row, autocommit=False) as c:
        c.execute("SELECT set_config('kb.actor_id', 'svc:dq-test', true)")
        c.execute("SELECT kb.record_dq_run(%s, %s)", (Jsonb(run), Jsonb(results)))
        c.commit()
    return run["run_id"]


def granted(client, user: str) -> set[str]:  # type: ignore[no-untyped-def]
    eng = client.app.state.kb.qa().t2s.engine
    return set(eng.policy.granted_tables(IDP.resolve(user)))


@pytest.mark.parametrize("user", USERS)
def test_rules_and_knowledge_are_scoped_to_the_caller(client, dq_run, user):
    r = client.get("/api/v1/quality", headers={"X-KB-User": user})
    assert r.status_code == 200, r.text
    body = r.json()
    tables = {x["table_name"] for x in body["data"]["rules"]}
    bus = {k["bu"] for k in body["knowledge"]}
    ident = IDP.resolve(user)
    if OPS_GROUP in ident.groups:
        assert tables == set(TABLES) and body["scope"] == "all"
    else:
        assert tables == granted(client, user), user  # exactly the granted tables: none hidden, none extra
        assert bus <= set(IDP.readable_bus(ident)), user
        if not tables:
            assert body["data"]["run"] is None
    for x in body["data"]["rules"]:
        assert set(x) == {"rule_id", "table_name", "rule_kind", "column_name", "checked", "failed", "tolerance",
                          "passed", "error"}  # fmt: skip


def test_a_user_with_no_groups_sees_nothing(client, dq_run):
    body = client.get("/api/v1/quality", headers={"X-KB-User": "nobody"}).json()
    assert body["data"]["rules"] == [] and body["knowledge"] == []


def test_retail_sees_credit_not_wealth_and_the_latest_run(client, dq_run):
    body = client.get("/api/v1/quality", headers={"X-KB-User": "ret.user1"}).json()
    tables = {x["table_name"] for x in body["data"]["rules"]}
    assert "mart_credit.loan" in tables and "mart_wealth.portfolio" not in tables
    assert str(body["data"]["run"]["run_id"]) == dq_run and body["data"]["tables_not_shown"] == 1
