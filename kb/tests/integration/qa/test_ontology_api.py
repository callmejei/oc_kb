"""Phase 7 star map API over the REAL local graph store and index: the BU overlay never crosses BUs (every
user), entity lookups are scoped and re-checked by the index ACL, and no SPARQL can be smuggled in."""

from __future__ import annotations

import pytest

from kb.adapters.local.identity import YamlIdentityProvider
from kb.gateway.acl import build_access_filter

IDP = YamlIdentityProvider("config/users.yaml")
USERS = [u.user_id for u in IDP.all_users()]


def get(client, path: str, user: str | None, **params):  # type: ignore[no-untyped-def]
    headers = {"X-KB-User": user} if user else {}
    return client.get("/api/v1/ontology" + path, headers=headers, params=params)


@pytest.fixture(scope="module")
def overlays(client) -> dict[str, dict]:  # type: ignore[type-arg]
    return {u: get(client, "/overlay", u).json() for u in USERS}


@pytest.mark.parametrize("user", USERS)
def test_overlay_never_crosses_bus(overlays, user):
    body = overlays[user]
    readable = IDP.readable_bus(IDP.resolve(user))
    assert set(body["bus"]) == readable
    assert {e["bu"] for e in body["entities"]} <= readable
    assert all(e["iri"].startswith(tuple(f"https://kb.local/id/{b}/" for b in readable)) for e in body["entities"])


def test_overlay_is_not_vacuous_and_reports_mapping_status(overlays):
    retail = overlays["ret.user1"]
    assert {e["bu"] for e in retail["entities"]} == {"retail", "shared"}
    assert retail["counts"].get("exact", 0) > 0 and sum(retail["counts"].values()) == len(retail["entities"])
    assert overlays["nobody"]["entities"] == [] and overlays["nobody"]["bus"] == []


def test_entity_of_another_bu_is_unknown(client, overlays):
    wealth_entity = next(e["iri"] for e in overlays["wlth.user1"]["entities"] if e["bu"] == "wealth")
    assert get(client, "/entity", "ret.user1", iri=wealth_entity).status_code == 404
    ok = get(client, "/entity", "wlth.user1", iri=wealth_entity)
    assert ok.status_code == 200 and ok.json()["bu"] == "wealth"


def test_entity_sources_are_documents_the_caller_can_open(client, overlays):
    ent = next(e for e in overlays["ret.user1"]["entities"] if e["bu"] == "retail" and e["chunks"] > 0)
    body = get(client, "/entity", "ret.user1", iri=ent["iri"]).json()
    assert body["sources"], body
    index = client.app.state.kb.chunk_index
    for user, expect in (("ret.user1", True), ("corp.user1", False)):
        flt = build_access_filter(IDP.resolve(user))
        for s in body["sources"]:  # the inspector's own authority: the index ACL (D-049)
            assert (s["version_id"] in index.visible_versions(s["doc_id"], flt)) is expect, (user, s)


@pytest.mark.parametrize(
    "iri",
    [
        "https://kb.local/id/retail/x> ?p ?o } #",
        "https://kb.local/id/retail/0123456789abcdef> . } UNION { ?s ?p ?o",
        "https://kb.local/id/wealth/../retail/0123456789abcdef",
        "urn:kb:graph:wealth",
    ],
)
def test_injection_shaped_iris_never_reach_sparql(client, iri):
    assert get(client, "/entity", "data.steward", iri=iri).status_code in (404, 422)


def test_fibo_artefact_needs_a_signed_in_user(client):
    assert get(client, "/fibo", None).status_code == 401
    r = get(client, "/fibo", "nobody")  # public standard: any signed-in user, even with no groups
    assert r.status_code in (200, 404)  # 404 only if the artefact has not been built
    if r.status_code == 200:
        body = r.json()
        assert body["release"] and len(body["nodes"]) == len(body["pos"]) and body["counts"]["C"] > 1000
        assert not any(n["iri"].startswith("https://kb.local/id/") for n in body["nodes"] if "iri" in n)
