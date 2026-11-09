"""Phase 8 explorer API over the REAL local index, graph store and marts: every view is scoped to the caller.
Search never returns another BU's canary (every user x every canary), the space inventory follows the index
ACL (a private upload is seen by its uploader only), catalog views list granted tables only."""

from __future__ import annotations

import pytest

from kb.adapters.local.identity import YamlIdentityProvider
from synthetic.world import CANARIES

IDP = YamlIdentityProvider("config/users.yaml")
USERS = [u.user_id for u in IDP.all_users()]


def get(client, path: str, user: str, **params):  # type: ignore[no-untyped-def]
    return client.get("/api/v1/explorer" + path, headers={"X-KB-User": user}, params=params)


def post(client, path: str, user: str, body: dict):  # type: ignore[no-untyped-def,type-arg]
    return client.post("/api/v1/explorer" + path, headers={"X-KB-User": user}, json=body)


def readable(user: str) -> set[str]:
    return set(IDP.readable_bus(IDP.resolve(user)))


def full_texts(live_index, results) -> str:  # type: ignore[no-untyped-def]
    """Whole chunk text of every result, from the index (the snippet is only a window)."""
    ids = [r["chunk_id"] for r in results]
    return " ".join(d["content"] for d in live_index.docs({"terms": {"chunk_id": ids}})) if ids else ""


# ------------------------------------------------------------------------------------------------ search
@pytest.mark.parametrize("user", USERS)
def test_search_never_returns_another_bus_canary(client, live_index, user):
    mine = readable(user)
    for c in CANARIES:
        r = post(client, "/search", user, {"query": c.question})
        assert r.status_code == 200, r.text
        res = r.json()["results"]
        assert {x["bu"] for x in res} <= mine, (user, c.canary_id)
        if c.bu not in mine:
            assert c.secret not in full_texts(live_index, res), (user, c.canary_id)


def test_search_finds_own_canary_and_nobody_gets_nothing(client, live_index):
    retail = next(c for c in CANARIES if c.bu == "retail")
    hits = post(client, "/search", "ret.user1", {"query": retail.question}).json()["results"]
    assert retail.secret in full_texts(live_index, hits)  # the leak check above is not vacuous
    assert post(client, "/search", "nobody", {"query": retail.question}).json()["results"] == []


def test_search_is_audited_with_hash_and_chunk_ids_not_text(client):
    body = post(client, "/search", "ret.user1", {"query": "loan-to-value limits for home loans"}).json()
    with client.app.state.kb.registry.transaction("ret.user1") as tx:
        row = tx.execute(
            "SELECT actor_id, action, object_id, details FROM kb.audit_log WHERE audit_id = %s", (body["audit_id"],)
        ).fetchone()
    assert row["actor_id"] == "ret.user1" and row["action"] == "SEARCH" and row["object_id"] == body["query_id"]
    assert row["details"]["result_chunk_ids"] == [r["chunk_id"] for r in body["results"]]
    assert "loan-to-value" not in str(row["details"])


def test_search_spaces_narrow_never_widen(client):
    res = post(client, "/search", "ret.user1", {"query": "fund codename", "spaces": ["wealth"]}).json()["results"]
    assert res == []
    res = post(client, "/search", "data.steward", {"query": "policy", "spaces": ["shared"]}).json()["results"]
    assert res and {r["bu"] for r in res} == {"shared"}


# ------------------------------------------------------------------------------------------------ inventory
@pytest.fixture(scope="module")
def inventories(client) -> dict[str, dict]:  # type: ignore[type-arg]
    return {u: get(client, "/spaces/inventory", u).json() for u in USERS}


@pytest.mark.parametrize("user", USERS)
def test_inventory_follows_readable_bus(inventories, user):
    inv = inventories[user]
    assert set(inv["bus"]) == readable(user)
    for s in inv["spaces"]:
        assert all(d["bu"] == s["bu"] for d in s["documents"])
        assert all("acl_groups" not in d for d in s["documents"])  # group names never leave the gateway


def test_private_uploads_are_in_their_uploaders_inventory_only(inventories):
    owners: dict[str, set[str]] = {}
    for user, inv in inventories.items():
        docs = [d for s in inv["spaces"] for d in s["documents"]] + inv["other_visible"]
        for d in docs:
            if d["private"]:
                owners.setdefault(d["doc_id"], set()).add(user)
    assert owners, "the live world has private uploads (Phase 2/6 scenarios)"
    assert all(len(u) == 1 for u in owners.values()), owners


def test_inventory_is_not_vacuous(inventories):
    assert inventories["ret.user1"]["spaces"][0]["count"] > 0
    assert inventories["nobody"] == {"bus": [], "spaces": [], "other_visible": []}


# ------------------------------------------------------------------------------------------------ catalog
@pytest.mark.parametrize("user", USERS)
def test_marts_and_table_map_list_granted_tables_only(client, user):
    pol = client.app.state.kb.qa().t2s.engine.policy
    ident = IDP.resolve(user)
    granted = set(pol.granted_tables(ident))
    m = get(client, "/catalog/marts", user).json()
    assert {t["table"] for t in m["tables"]} == granted
    assert m["not_granted"] == len(pol.catalog) - len(granted)
    for t in m["tables"]:
        assert {c["name"]: c["mask"] for c in t["columns"] if c["mask"]} == pol.masked_columns(ident, t["table"])
    tm = get(client, "/catalog/table-map", user).json()
    assert {e["table"] for e in tm["entries"]} <= granted and tm["editable"] is False
    assert len(tm["entries"]) + tm["hidden"] == len(client.app.state.kb.qa().t2s.table_map.entries)


def test_corporate_user_sees_no_wealth_mart(client):
    m = get(client, "/catalog/marts", "corp.user1").json()
    assert "mart_wealth.portfolio" not in {t["table"] for t in m["tables"]}
    assert get(client, "/catalog/marts", "nobody").json()["tables"] == []


@pytest.mark.parametrize("user", ["ret.user1", "corp.user1", "wlth.user1", "nobody"])
def test_graph_stats_cover_readable_bus_only(client, user):
    g = get(client, "/catalog/graphs", user).json()
    assert set(g["bus"]) == readable(user) and {x["bu"] for x in g["graphs"]} == readable(user)


# ------------------------------------------------------------------------------------------------ governance
def test_my_access_reports_the_callers_own_grants(client):
    a = get(client, "/governance/my-access", "ret.maker").json()
    ident = IDP.resolve("ret.maker")
    assert set(a["groups"]) == set(ident.groups) and a["maker_bus"] == ["retail"]
    assert {t["table"] for t in a["tables"]} == set(client.app.state.kb.qa().t2s.engine.policy.granted_tables(ident))


def test_activity_is_the_callers_own_rows(client):
    post(client, "/search", "corp.user1", {"query": "trade finance fees"})
    rows = get(client, "/governance/activity", "corp.user1").json()
    assert rows and rows[0]["action"] == "SEARCH"
    with client.app.state.kb.registry.transaction("corp.user1") as tx:
        actors = tx.execute(
            "SELECT DISTINCT actor_id FROM kb.audit_log WHERE audit_id = ANY(%s)", ([r["audit_id"] for r in rows],)
        ).fetchall()
    assert [a["actor_id"] for a in actors] == ["corp.user1"]


def test_approvals_only_for_governance_bus(client):
    assert get(client, "/governance/approvals", "ret.user1").json() == {"bus": [], "requests": []}
    body = get(client, "/governance/approvals", "ret.checker").json()
    assert body["bus"] == ["retail"] and all(r["bu"] == "retail" for r in body["requests"])
