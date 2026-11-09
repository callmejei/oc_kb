"""End-to-end leak matrix with REAL model answers (slow; `make leak` / `kb.ps1 leak`).

Every user x every secret's natural question through retrieval + rerank + qwen3. A user who may not see
a secret must never get it in the answer or in any citation. Part of Phase 3 acceptance.
"""

from __future__ import annotations

import pytest

from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.agent.answer import answer
from kb.gateway.acl import build_access_filter

pytestmark = pytest.mark.leak_llm
IDP = YamlIdentityProvider("config/users.yaml")
USERS = [u.user_id for u in IDP.all_users()]


@pytest.fixture(scope="module")
def llm(qa_settings):
    return build(qa_settings, "llm")


@pytest.fixture(scope="module")
def truth(manifest, live_index):
    out = []
    for c in manifest["canaries"]:
        out.append({"secret": c["secret"], "q": c["question"]})
    for u in manifest["upload_scenarios"]:
        if "secret" in u:
            out.append({"secret": u["secret"], "q": f"What is {u['secret']}?"})
    for t in out:
        holders = live_index.docs(
            {"bool": {"filter": [{"match_phrase": {"content": t["secret"]}}, {"term": {"is_current": True}}]}}
        )
        t["acls"] = [set(h["acl_groups"]) for h in holders]
    return [t for t in out if t["acls"]]


@pytest.mark.parametrize("user", USERS)
def test_no_secret_in_any_answer(user, retriever, llm, truth):
    ident = IDP.resolve(user)
    flt = build_access_filter(ident)
    leaks = []
    for t in truth:
        if any(acl & ident.acl_principals for acl in t["acls"]):
            continue  # allowed: not a leak question for this user
        r = retriever.retrieve(t["q"], flt)
        a = answer(t["q"], r.context, llm)
        if t["secret"].lower() in a.text.lower():
            leaks.append((t["secret"], a.text[:120]))
        assert {c.chunk_id for c in a.citations} <= {c.chunk.chunk_id for c in r.context}
    assert leaks == [], f"LEAK in answers for {user}: {leaks}"
