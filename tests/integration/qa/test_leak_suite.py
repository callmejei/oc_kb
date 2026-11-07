"""Phase 3 acceptance: ACL leak suite. Every user x every canary, zero leaks.

Ground truth comes from the index itself: for each secret, which chunks hold it and with which ACL.
For each user and secret, two queries run through the real hybrid pipeline with the user's mandatory
filter: the natural question and an adversarial query for the secret token. Assertions:
  - every retrieved chunk is one the user may see (ACL intersects the user's principals);
  - a user who may NOT see the secret never gets it in any retrieved chunk or LLM context;
  - a user who MAY see it does get it (the suite is not vacuous).
Checks run over the retrievers' raw output with k=50 (the LLM context is always a subset of it), plus
test_context_is_subset_of_candidates on the full pipeline. Tests marked leak_llm (`make leak`) repeat
the matrix end to end with real model answers.
"""

from __future__ import annotations

from typing import Any

import pytest

from kb.adapters.local.identity import YamlIdentityProvider
from kb.gateway.acl import build_access_filter

IDP = YamlIdentityProvider("config/users.yaml")
USERS = [u.user_id for u in IDP.all_users()]


@pytest.fixture(scope="module")
def secrets_truth(manifest, live_index) -> list[dict[str, Any]]:
    out = []
    candidates = [
        {"secret": c["secret"], "question": c["question"], "bu": c["bu"], "kind": "canary"}
        for c in manifest["canaries"]
    ]
    candidates += [
        {"secret": u["secret"], "question": f"What is {u['secret']}?", "bu": None, "kind": "upload"}
        for u in manifest["upload_scenarios"]
        if "secret" in u
    ]
    for c in candidates:
        holders = live_index.docs(
            {"bool": {"filter": [{"match_phrase": {"content": c["secret"]}}, {"term": {"is_current": True}}]}}
        )
        if not holders:
            if c["kind"] == "canary":
                pytest.fail(f"canary {c['secret']} not in the live index")
            continue  # this upload scenario was not run against the live world
        out.append({**c, "acls": [set(h["acl_groups"]) for h in holders]})
    assert sum(1 for c in out if c["kind"] == "canary") == len(manifest["canaries"])
    return out


def allowed(user: str, truth: dict[str, Any]) -> bool:
    p = IDP.resolve(user).acl_principals
    return any(acl & p for acl in truth["acls"])


def test_canary_chunks_carry_exactly_their_bu_acl(secrets_truth):
    for t in secrets_truth:
        if t["kind"] == "canary":
            assert all(acl == {f"GRP_KB_{t['bu'].upper()}_READ"} for acl in t["acls"]), t["secret"]


def test_expected_audience_per_canary(secrets_truth):
    """Sanity: who may see a canary is exactly who reads its BU (directly or via nested groups)."""
    for t in secrets_truth:
        if t["kind"] != "canary":
            continue
        expect = {u for u in USERS if t["bu"] in IDP.readable_bus(IDP.resolve(u))}
        assert {u for u in USERS if allowed(u, t)} == expect, t["secret"]


@pytest.mark.parametrize("user", USERS)
def test_zero_leaks_for_user(user, retriever, secrets_truth):
    ident = IDP.resolve(user)
    flt = build_access_filter(ident)
    leaks, misses = [], []
    for t in secrets_truth:
        found = False
        for q in (t["question"], t["secret"]):
            chunks = retriever.candidates(q, flt, k=50)  # superset of anything the LLM could be shown
            for ch in chunks:  # invariant: nothing outside the user's ACL ever comes back
                assert set(ch.chunk.acl_groups) & ident.acl_principals, (user, ch.chunk.chunk_id)
                assert ch.chunk.is_current and ch.chunk.source_active
            hit = any(t["secret"] in ch.chunk.text for ch in chunks)
            found |= hit
            if hit and not allowed(user, t):
                leaks.append((t["secret"], q))
        if allowed(user, t) and not found:
            misses.append(t["secret"])
    assert leaks == [], f"LEAK for {user}: {leaks}"
    assert misses == [], f"{user} may see {misses} but retrieval did not return them (suite would be vacuous)"


@pytest.mark.parametrize("user", ["ret.user1", "corp.user2", "wlth.checker", "grp.reader"])
def test_context_is_subset_of_candidates(user, retriever, secrets_truth):
    """The full pipeline (fusion + rerank) can only narrow what the retrievers returned."""
    flt = build_access_filter(IDP.resolve(user))
    for t in secrets_truth[:4]:
        full = retriever.retrieve(t["question"], flt)
        cand = {c.chunk.chunk_id for c in retriever.candidates(t["question"], flt)}
        assert {c.chunk.chunk_id for c in full.context} <= cand
        assert {c.chunk.chunk_id for c in full.retrieved} <= cand
