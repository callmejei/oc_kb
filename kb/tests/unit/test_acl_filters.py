"""The filters the gateway builds: BU spaces only narrow; the inspection filter keeps the ACL clause."""

from __future__ import annotations

from kb.adapters.local.es_index import render_filter
from kb.adapters.local.identity import YamlIdentityProvider
from kb.gateway.acl import build_access_filter, build_inspection_filter

IDP = YamlIdentityProvider("config/users.yaml")


def test_spaces_add_a_bu_term_and_keep_every_mandatory_clause():
    ident = IDP.resolve("ret.user1")
    plain = render_filter(build_access_filter(ident))
    spaced = render_filter(build_access_filter(ident, ["wealth"]))  # a BU the user cannot read
    assert all(clause in spaced for clause in plain)  # ACL, is_current, source_active all still there
    assert {"terms": {"bu": ["wealth"]}} in spaced  # and an extra BU term: the result is the intersection


def test_no_groups_still_matches_nothing_with_spaces():
    assert render_filter(build_access_filter(IDP.resolve("nobody"), ["retail"]))[0] == {
        "terms": {"acl_groups": ["user:nobody"]}
    }


def test_inspection_filter_keeps_acl_and_bus_and_is_empty_without_governance():
    ident = IDP.resolve("ret.maker")
    f = render_filter(build_inspection_filter(ident, {"retail"}))
    assert {"terms": {"acl_groups": sorted(ident.acl_principals)}} in f and {"terms": {"bu": ["retail"]}} in f
    assert {"term": {"is_current": True}} not in f  # superseded / excluded versions are inspectable
    assert render_filter(build_inspection_filter(IDP.resolve("ret.user1"), set())) == [{"match_none": {}}]
