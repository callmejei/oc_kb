"""users.yaml satisfies CLAUDE.md section 6."""

from __future__ import annotations

from collections import Counter

import pytest

from kb.adapters.local.identity import UnknownUserError, YamlIdentityProvider


@pytest.fixture(scope="module")
def idp() -> YamlIdentityProvider:
    return YamlIdentityProvider("config/users.yaml")


@pytest.mark.parametrize("bu", ["retail", "corporate", "wealth"])
def test_each_bu_has_maker_checker_and_two_ordinary_users(idp, bu):
    makers = [u for u in idp.all_users() if (bu, "MAKER") in idp.governance_roles(u)]
    checkers = [u for u in idp.all_users() if (bu, "CHECKER") in idp.governance_roles(u)]
    ordinary = [u for u in idp.all_users() if u.bu == bu and not idp.governance_roles(u)]
    assert len(makers) >= 1 and len(checkers) >= 1 and len(ordinary) >= 2
    assert not {m.user_id for m in makers} & {c.user_id for c in checkers}, "maker and checker must differ"


def test_cross_bu_group_policy_reader(idp):
    u = idp.resolve("grp.reader")
    assert idp.readable_bus(u) == {"shared"}
    assert not idp.governance_roles(u)


def test_user_with_no_groups(idp):
    u = idp.resolve("nobody")
    assert u.groups == frozenset()
    assert u.acl_principals == {"user:nobody"}


def test_acl_principals_include_private_principal(idp):
    assert "user:ret.user1" in idp.resolve("ret.user1").acl_principals


def test_no_ordinary_user_reads_another_bu(idp):
    for u in idp.all_users():
        if u.bu in ("retail", "corporate", "wealth"):
            assert idp.readable_bus(u) <= {u.bu, "shared"}, u.user_id


def test_unique_ids_and_unknown_user(idp):
    ids = [u.user_id for u in idp.all_users()]
    assert not [i for i, n in Counter(ids).items() if n > 1]
    with pytest.raises(UnknownUserError):
        idp.resolve("intruder")
