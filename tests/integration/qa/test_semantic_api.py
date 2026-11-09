"""Phase 9c/9d leak checks over every user (real local registry + marts, the gateway's own engine):

- the semantic catalogue lists only metrics on tables the user is granted;
- governance endpoints (products, metric governance) refuse everyone outside the `data` domain;
- a metric computed for a user never returns rows outside that user's row filter (run as the user, no LLM).
"""

from __future__ import annotations

import pytest

from kb.adapters.local.identity import YamlIdentityProvider
from kb.gateway.routes_ops import OPS_GROUP
from kb.semantic.model import match
from kb.semantic.store import MetricSource, run_metric

IDP = YamlIdentityProvider("config/users.yaml")
USERS = [u.user_id for u in IDP.all_users()]
DATA_GOVERNORS = {"data.maker", "data.checker"}


def engine(client):  # type: ignore[no-untyped-def]
    return client.app.state.kb.qa().t2s.engine


@pytest.mark.parametrize("user", USERS)
def test_catalogue_lists_only_metrics_on_granted_tables(client, user):
    r = client.get("/api/v1/semantic/metrics", headers={"X-KB-User": user})
    assert r.status_code == 200, r.text
    ident = IDP.resolve(user)
    granted = set(engine(client).policy.granted_tables(ident))
    tables = {m["lineage"]["table"] for m in r.json()["metrics"]}
    if OPS_GROUP not in ident.groups:
        assert tables <= granted, user


@pytest.mark.parametrize("user", USERS)
def test_governance_endpoints_refuse_everyone_outside_the_data_domain(client, user):
    for path in ("/api/v1/products", "/api/v1/products/requests", "/api/v1/semantic/governance"):
        code = client.get(path, headers={"X-KB-User": user}).status_code
        assert code == (200 if user in DATA_GOVERNORS else 403), (user, path, code)


@pytest.mark.parametrize("user", USERS)
def test_a_metric_never_returns_rows_outside_the_users_row_filter(client, user):
    from kb.config import load_settings

    eng = engine(client)
    src = MetricSource.from_settings(load_settings(), eng)
    assert src is not None
    metrics = src.current()
    ident = IDP.resolve(user)
    m = match("loan count by bu", metrics)
    assert m is not None
    ran = run_metric(eng, m, ident, 50)
    allowed = {"retail": "GRP_DATA_CREDIT_RETAIL", "corporate": "GRP_DATA_CREDIT_CORPORATE"}
    if ran is None:
        assert "mart_credit.loan" not in eng.policy.granted_tables(ident), user
        return
    bus = {row[0] for row in ran[0].rows}
    assert bus <= {bu for bu, g in allowed.items() if g in ident.groups}, (user, bus)
