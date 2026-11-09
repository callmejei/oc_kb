"""Phase 9b products API (kb_test registry): data-domain governors only; specs validated against the real mart
catalogue (row filters sanitised); the checker sees a diff that marks widening; a stale approval is a 409."""

from __future__ import annotations

import pytest

PRODUCT = "mart_wealth.portfolio"
SPEC = {
    "grants": ["GRP_DATA_WEALTH"],
    "row_filters": [{"groups": ["GRP_DATA_WEALTH"], "filter": "bu='wealth'"}],
    "column_masks": {"client_ref": {"mask": "hash", "unless_groups": []}},
}
BODY = {"product_id": PRODUCT, "owner": "Wealth data (synthetic)", "description": "Portfolios",
        "classification": "CONFIDENTIAL", "refresh": "daily", "spec": SPEC}  # fmt: skip


def call(client, method: str, path: str, user: str, body: dict | None = None):  # type: ignore[no-untyped-def,type-arg]
    return client.request(method, "/api/v1/products" + path, headers={"X-KB-User": user}, json=body)


@pytest.mark.parametrize("user", ["ret.user1", "ret.maker", "ret.checker", "ops.analyst", "data.steward", "nobody"])
def test_only_data_domain_governors_get_in(client, user):
    assert call(client, "GET", "", user).status_code == 403
    assert call(client, "POST", "/drafts", user, BODY).status_code == 403


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"product_id": "mart_x.nope"}, "not a mart table"),
        ({"spec": {**SPEC, "row_filters": [{"groups": ["GRP_DATA_WEALTH"], "filter": "bu IN (SELECT bu FROM t)"}]}},
         "subquery"),
        ({"spec": {**SPEC, "row_filters": [{"groups": ["GRP_DATA_WEALTH"], "filter": "secret = 1"}]}}, "not a column"),
        ({"spec": {**SPEC, "column_masks": {"ghost": {"mask": "redact"}}}}, "unknown column"),
    ],
)  # fmt: skip
def test_bad_drafts_are_refused_with_the_reason(client, patch, needle):
    r = call(client, "POST", "/drafts", "data.maker", {**BODY, **patch})
    assert r.status_code == 422 and needle in r.text


def test_maker_to_checker_with_a_diff_then_stale_approval_is_refused(client):
    r = call(client, "POST", "/drafts", "data.maker", BODY)
    assert r.status_code == 201, r.text
    req = call(client, "POST", f"/drafts/{r.json()['revision_id']}/submit", "data.maker", {"comment": "onboard"}).json()
    pending = [
        x for x in call(client, "GET", "/requests", "data.checker").json() if x["request_id"] == req["request_id"]
    ]
    assert pending and pending[0]["widens"] and {"kind": "grant", "item": "GRP_DATA_WEALTH", "change": "added",
                                                 "widens": True} in pending[0]["diff"]  # fmt: skip
    assert pending[0]["proposed"]["row_filters"][0]["filter"] == "bu = 'wealth'"  # stored re-serialised
    assert call(client, "POST", f"/requests/{req['request_id']}/decide", "data.maker",
                {"approve": True}).status_code == 403  # fmt: skip
    ok = call(
        client, "POST", f"/requests/{req['request_id']}/decide", "data.checker", {"approve": True, "reason": "ok"}
    )
    assert ok.status_code == 200, ok.text

    # a widening change is drafted, then a restriction lands first: approving the old draft must fail (P6)
    wider = {**BODY, "spec": {**SPEC, "column_masks": {}}}
    rev = call(client, "POST", "/drafts", "data.maker", wider).json()["revision_id"]
    req2 = call(client, "POST", f"/drafts/{rev}/submit", "data.maker", {}).json()["request_id"]
    diff = next(x for x in call(client, "GET", "/requests", "data.checker").json() if x["request_id"] == req2)["diff"]
    assert {"kind": "mask", "item": "client_ref", "change": "mask removed", "widens": True} in diff
    r = call(client, "POST", f"/{PRODUCT}/restrict", "data.maker",
             {"action": "ADD_MASK", "reason": "pii review", "column": "customer_id", "mask": "hash"})  # fmt: skip
    assert r.status_code == 201 and r.json()["status"] == "EXECUTED"
    stale = call(client, "POST", f"/requests/{req2}/decide", "data.checker", {"approve": True, "reason": "ok"})
    assert stale.status_code == 409 and "redraft" in stale.text


def test_listing_shows_unregistered_marts_as_denied(client):
    body = call(client, "GET", "", "data.checker").json()
    assert body["checker"] and not body["maker"]
    assert set(body["unregistered"]) | {p["product_id"] for p in body["products"]} >= {PRODUCT, "mart_cust.customer"}
