"""Governance HTTP API: authentication, maker flows, checker flows."""

from __future__ import annotations

from fastapi.testclient import TestClient

from kb.adapters.local.identity import YamlIdentityProvider
from kb.config import load_settings
from kb.gateway.app import create_app

from helpers import API, CHECKER, MAKER, Api, onboard, register, revision_body, uid


# ---------------------------------------------------------------------------- auth
def test_missing_or_unknown_user_is_401(client):
    assert client.get(API + "/sources").status_code == 401
    assert client.get(API + "/sources", headers={"X-KB-User": "intruder"}).status_code == 401


def test_dev_header_refused_from_untrusted_client(registry):
    app = create_app(
        load_settings("local"),
        registry,
        YamlIdentityProvider("config/users.yaml"),
        trusted_clients=frozenset({"127.0.0.1"}),
    )
    with TestClient(app) as c:  # TestClient's client host is "testclient", not loopback
        assert c.get(API + "/sources", headers={"X-KB-User": "ret.maker"}).status_code == 403


def test_healthz(client):
    assert client.get("/healthz").json() == {"registry": True}


# ---------------------------------------------------------------------------- maker
def test_onboard_flow_end_to_end(api):
    created = register(api, "wealth")
    sid, rid = created["source"]["source_id"], created["revision"]["revision_id"]
    assert created["source"]["status"] == "DRAFT" and created["revision"]["created_by"] == "wlth.maker"
    req = api.call("POST", f"/revisions/{rid}/submit", "wlth.maker", {"comment": "new library"}).json()["request_id"]
    inbox = {i["request_id"]: i for i in api.call("GET", "/inbox", "wlth.checker").json()}
    assert inbox[req]["request_type"] == "ONBOARD_SOURCE"
    detail = api.call("GET", f"/requests/{req}", "wlth.checker").json()
    assert detail["current"] is None and detail["diff"]["acl_added"] == ["GRP_KB_WEALTH_READ"]
    r = api.call("POST", f"/requests/{req}/approve", "wlth.checker", {"reason": "checked folder and ACL"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED" and r.json()["checker_id"] == "wlth.checker"
    src = {s["source_id"]: s for s in api.call("GET", "/sources", "wlth.maker").json()}[sid]
    assert src["status"] == "ACTIVE" and str(src["current_revision_id"]) == rid


def test_change_flow_shows_acl_diff_and_supersedes(api):
    src = onboard(api, "retail")
    r = api.call(
        "POST",
        f"/sources/{src['source_id']}/revisions",
        "ret.maker",
        {"acl_groups": ["GRP_KB_RETAIL_READ", "GRP_KB_RETAIL_CHECKER"]},
    )
    assert r.status_code == 201 and r.json()["revision_no"] == 2
    # one open draft at a time
    assert api.call("POST", f"/sources/{src['source_id']}/revisions", "ret.maker", {}).status_code == 409
    req = api.call("POST", f"/revisions/{r.json()['revision_id']}/submit", "ret.maker", {}).json()["request_id"]
    detail = api.call("GET", f"/requests/{req}", "ret.checker").json()
    assert detail["request"]["request_type"] == "CHANGE_SOURCE"
    assert detail["diff"]["acl_added"] == ["GRP_KB_RETAIL_CHECKER"] and detail["diff"]["acl_removed"] == []
    assert [c["field"] for c in detail["diff"]["changes"]] == ["acl_groups"]
    api.call("POST", f"/requests/{req}/approve", "ret.checker", {})
    old = api.call("GET", f"/requests/{src['request_id']}", "ret.checker").json()
    assert old["request"]["status"] == "APPROVED"


def test_maker_of_other_bu_cannot_register_or_edit(api):
    body = {
        "source_id": uid("ret"),
        "bu": "retail",
        "display_name": "x",
        "sp_site_id": "s",
        "sp_drive_id": "d",
        "sp_folder_path": "/",
        "revision": revision_body("retail"),
    }
    assert api.call("POST", "/sources", "corp.maker", body).status_code == 403
    created = register(api, "retail")
    rid = created["revision"]["revision_id"]
    assert api.call("PATCH", f"/revisions/{rid}", "corp.maker", {"retention_class": "R1Y"}).status_code == 403
    assert api.call("POST", f"/revisions/{rid}/submit", "corp.maker", {}).status_code == 403


def test_acl_cannot_name_another_bus_groups(api):
    body = {
        "source_id": uid("ret"),
        "bu": "retail",
        "display_name": "x",
        "sp_site_id": "s",
        "sp_drive_id": "d",
        "sp_folder_path": "/",
        "revision": revision_body("retail", acl_groups=["GRP_KB_WEALTH_READ"]),
    }
    r = api.call("POST", "/sources", "ret.maker", body)
    assert r.status_code == 422 and "GRP_KB_WEALTH_READ" in r.text
    created = register(api, "retail")
    r = api.call(
        "PATCH",
        f"/revisions/{created['revision']['revision_id']}",
        "ret.maker",
        {"acl_groups": ["GRP_DATA_CREDIT_RETAIL"]},
    )
    assert r.status_code == 422


def test_submitted_revision_cannot_be_edited_via_api(api):
    created = register(api, "retail")
    rid = created["revision"]["revision_id"]
    api.call("POST", f"/revisions/{rid}/submit", "ret.maker", {})
    r = api.call("PATCH", f"/revisions/{rid}", "ret.maker", {"retention_class": "R1Y"})
    assert r.status_code == 403 and "locked" in r.text


def test_invalid_payloads_rejected(api):
    assert api.call("POST", "/sources", "ret.maker", {"source_id": "Bad Id!"}).status_code == 422
    body = {
        "source_id": uid("ret"),
        "bu": "retail",
        "display_name": "x",
        "sp_site_id": "s",
        "sp_drive_id": "d",
        "sp_folder_path": "/",
        "revision": revision_body("retail", acl_groups=[]),
    }
    assert api.call("POST", "/sources", "ret.maker", body).status_code == 422
    assert api.call("POST", "/sources", "ret.maker", {**body, "unexpected": 1}).status_code == 422


def test_duplicate_source_id_conflicts(api):
    created = register(api, "retail")
    body = {
        "source_id": created["source"]["source_id"],
        "bu": "retail",
        "display_name": "x",
        "sp_site_id": "s",
        "sp_drive_id": "d",
        "sp_folder_path": "/",
        "revision": revision_body("retail"),
    }
    assert api.call("POST", "/sources", "ret.maker", body).status_code == 409


def test_withdraw_only_by_maker(api):
    created = register(api, "retail")
    req = api.call("POST", f"/revisions/{created['revision']['revision_id']}/submit", "ret.maker", {}).json()
    assert api.call("POST", f"/requests/{req['request_id']}/withdraw", "ret.checker", {}).status_code == 403
    r = api.call("POST", f"/requests/{req['request_id']}/withdraw", "ret.maker", {"reason": "typo"})
    assert r.status_code == 200 and r.json()["status"] == "WITHDRAWN"
    assert api.call("POST", f"/requests/{req['request_id']}/approve", "ret.checker", {}).status_code == 403


# ---------------------------------------------------------------------------- checker
def test_inbox_requires_checker_role_and_is_bu_scoped(api: Api):
    assert api.call("GET", "/inbox", "ret.user1").status_code == 403
    assert api.call("GET", "/inbox", "nobody").status_code == 403
    created = register(api, "corporate")
    req = api.call("POST", f"/revisions/{created['revision']['revision_id']}/submit", MAKER["corporate"], {}).json()
    assert req["request_id"] in {i["request_id"] for i in api.call("GET", "/inbox", CHECKER["corporate"]).json()}
    assert req["request_id"] not in {i["request_id"] for i in api.call("GET", "/inbox", CHECKER["retail"]).json()}


def test_reject_requires_reason_and_approve_by_wrong_people_fails(api):
    created = register(api, "retail")
    req = api.call("POST", f"/revisions/{created['revision']['revision_id']}/submit", "ret.maker", {}).json()
    rid = req["request_id"]
    assert api.call("POST", f"/requests/{rid}/reject", "ret.checker", {}).status_code == 422
    assert api.call("POST", f"/requests/{rid}/approve", "ret.maker", {}).status_code == 403
    assert api.call("POST", f"/requests/{rid}/approve", "ret.user1", {}).status_code == 403
    assert api.call("POST", f"/requests/{rid}/approve", "corp.checker", {}).status_code == 403
    assert api.call("POST", f"/requests/{rid}/approve", "ret.checker", {}).status_code == 200


def test_request_detail_hidden_from_other_bus(api):
    created = register(api, "wealth")
    req = api.call("POST", f"/revisions/{created['revision']['revision_id']}/submit", "wlth.maker", {}).json()
    for user in ("ret.checker", "corp.maker", "ret.user1", "nobody"):
        assert api.call("GET", f"/requests/{req['request_id']}", user).status_code == 404
    assert api.call("GET", "/requests/00000000-0000-0000-0000-000000000000", "wlth.checker").status_code == 404


def test_sources_list_scoped_to_governance_bus(api):
    src = onboard(api, "shared")
    assert src["source_id"] in {s["source_id"] for s in api.call("GET", "/sources", "shr.checker").json()}
    assert src["source_id"] not in {s["source_id"] for s in api.call("GET", "/sources", "ret.maker").json()}
    assert api.call("GET", "/sources", "grp.reader").json() == []
