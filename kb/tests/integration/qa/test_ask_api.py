"""Gateway /ask and /auth over the live index; audit goes to the throwaway kb_test registry."""

from __future__ import annotations

import re
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg

from kb.agent.answer import NO_CONTEXT
from kb.agent.service import AskService
from kb.config import REPO_ROOT
from kb.gateway.app import create_app
from kb.gateway.auth import AUDIENCE, ISSUER, generate_local_users

from helpers import RecordingLLM

SECRET = "t" * 48


@pytest.fixture(scope="module")
def users_file(tmp_path_factory, idp):
    d = tmp_path_factory.mktemp("secrets")
    pw = generate_local_users([u.user_id for u in idp.all_users()], d)
    return d / "local_users.hash.json", pw


@pytest.fixture(scope="module")
def llm():
    return RecordingLLM()


@pytest.fixture(scope="module")
def app_client(qa_settings, registry, idp, live_index, retriever, users_file, llm):
    gw = dict(qa_settings.gateway) | {
        "auth": "local_jwt",
        "jwt_secret": SECRET,
        "local_users": str(users_file[0]),
        "trusted_clients": [],
    }
    settings = qa_settings.model_copy(update={"gateway": gw})
    app = create_app(
        settings,
        registry,
        idp,
        chunk_index=live_index,
        qa_factory=lambda: AskService(retriever, llm, registry, qa_settings.retrieval),
    )
    with TestClient(app) as c:
        yield c


def login(client, user, pw) -> str:
    r = client.post("/api/v1/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def ask(client, token, q):  # type: ignore[no-untyped-def]
    return client.post("/api/v1/ask", json={"question": q}, headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------------------------- auth
def test_login_and_token_required(app_client, users_file):
    pw = users_file[1]
    assert app_client.post("/api/v1/auth/login", json={"username": "ret.user1", "password": "wrong"}).status_code == 401
    r_unknown = app_client.post("/api/v1/auth/login", json={"username": "intruder", "password": "x"})
    assert r_unknown.status_code == 401 and r_unknown.json()["detail"] == "invalid username or password"
    assert app_client.post("/api/v1/ask", json={"question": "fees?"}).status_code == 401
    tok = login(app_client, "ret.user1", pw["ret.user1"])
    assert ask(app_client, tok, "What is the late payment fee?").status_code == 200


def test_bad_tokens_and_dev_header_refused(app_client):
    now = int(time.time())
    expired = jwt.encode(
        {"sub": "ret.user1", "iat": now - 999, "exp": now - 10, "iss": ISSUER, "aud": AUDIENCE},
        SECRET,
        algorithm="HS256",
    )
    forged = jwt.encode(
        {"sub": "ret.checker", "iat": now, "exp": now + 60, "iss": ISSUER, "aud": AUDIENCE}, "f" * 48, algorithm="HS256"
    )
    for t in (expired, forged, "garbage"):
        assert ask(app_client, t, "fees?").status_code == 401
    # no trusted test clients configured: the dev header is refused
    r = app_client.post("/api/v1/ask", json={"question": "fees?"}, headers={"X-KB-User": "ret.checker"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------------------------- ask
def test_ask_cites_only_documents_the_user_may_see(app_client, users_file, llm, idp):
    tok = login(app_client, "corp.user1", users_file[1]["corp.user1"])
    r = ask(app_client, tok, "What is the maximum loan-to-value for commercial office property?")
    body = r.json()
    assert r.status_code == 200 and body["grounded"] and body["citations"]
    prompt = llm.calls[-1][1].content
    assert "COPPERFINCH" not in prompt and "BLUEHERON" not in prompt
    for c in body["citations"]:
        assert c["source_id"] in ("corporate-kb", "shared-kb") and c["version_no"] and c["title"]


def test_user_without_groups_gets_no_content_and_no_llm_call(app_client, users_file, llm):
    n = len(llm.calls)
    tok = login(app_client, "nobody", users_file[1]["nobody"])
    body = ask(app_client, tok, "What is the late payment fee?").json()
    assert body["answer"] == NO_CONTEXT and body["citations"] == [] and len(llm.calls) == n


def test_private_upload_only_in_uploaders_context(app_client, users_file, llm, live_index):
    if not live_index.docs({"match_phrase": {"content": "TEALSPARROW"}}):
        pytest.skip("live world has no private upload (run the Phase 2 live upload)")
    q = "What is the draft savings campaign codename?"
    for user, expect in (("ret.user1", True), ("ret.user2", False), ("ret.checker", False), ("wlth.user1", False)):
        before = len(llm.calls)
        ask(app_client, login(app_client, user, users_file[1][user]), q)
        prompt = llm.calls[-1][1].content if len(llm.calls) > before else ""
        assert ("TEALSPARROW" in prompt) is expect, user


def test_every_question_is_audited_without_plain_text(app_client, users_file, as_admin, as_api):
    tok = login(app_client, "wlth.user1", users_file[1]["wlth.user1"])
    body = ask(app_client, tok, "What is the advance ratio for blue-chip equities?").json()
    with as_admin() as c:
        a = c.execute(
            "SELECT actor_id, action, object_id, details FROM kb.audit_log WHERE audit_id = %s", (body["audit_id"],)
        ).fetchone()
    assert (a["actor_id"], a["action"], a["object_id"]) == ("wlth.user1", "QUERY", body["query_id"])
    d = a["details"]
    assert (
        "GRP_KB_WEALTH_READ" in d["filter"]["acl_principals"]
        and d["filter"]["is_current"]
        and d["filter"]["source_active"]
    )
    assert d["retrieved_chunk_ids"] and set(d["cited_chunk_ids"]) <= set(d["context_chunk_ids"]) <= set(
        d["retrieved_chunk_ids"]
    )
    assert "blue-chip" not in str(d) and len(d["question_sha256"]) == 64  # no plain text in the audit row
    with as_api("wlth.user1") as c, pytest.raises(pg.InsufficientPrivilege):
        c.execute("SELECT question FROM kb.query_log")  # the gateway role cannot read questions back
    with as_admin() as c:
        c.execute("SET ROLE kb_auditor")
        q = c.execute("SELECT user_id, question FROM kb.query_log WHERE query_id = %s", (body["query_id"],)).fetchone()
    assert q["user_id"] == "wlth.user1" and "blue-chip" in q["question"]


def test_audit_actor_cannot_be_forged(as_api, as_admin):
    with as_api("ret.user1") as c:
        row = c.execute(
            "INSERT INTO kb.audit_log (actor_id, action, object_type) VALUES ('ret.checker', 'QUERY', "
            "'query') RETURNING actor_id"
        ).fetchone()
    assert row["actor_id"] == "ret.user1"


def test_only_the_gateway_builds_access_filters():
    """Rule 3/4: production code constructs AccessFilter in exactly one place."""
    hits = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "src").rglob("*.py")
        if re.search(r"\bAccessFilter\(", p.read_text(encoding="utf-8"))
    ]
    assert hits == ["src/kb/gateway/acl.py"]
