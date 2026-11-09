"""Migration 011 (Phase 9b, D-066): data-product policies are governed in the DATABASE.

P1 maker <> checker; P2 locked revisions; P3 pipeline never governs; P4 widening needs a checker; P5 restriction is
single-actor and derived by the database (it can only narrow); P6 a stale approval cannot undo a restriction;
P7 audit + policy version. Runs against the throwaway kb_test database; product ids are unique per test.
"""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from psycopg.types.json import Jsonb

SPEC = {
    "grants": ["GRP_DATA_A", "GRP_DATA_B"],
    "row_filters": [
        {"groups": ["GRP_DATA_A"], "filter": "bu = 'retail'"},
        {"groups": ["GRP_DATA_A", "GRP_DATA_B"], "filter": "bu = 'corporate'"},
    ],
    "column_masks": {"email": {"mask": "redact", "unless_groups": ["GRP_DATA_B"]}},
}


def pid() -> str:
    return f"mart_t.p{uuid.uuid4().hex[:10]}"


def one(conn, sql, *params):  # type: ignore[no-untyped-def]
    row = conn.execute(sql, params).fetchone()
    return next(iter(row.values()))


def draft(c, product, spec=SPEC):  # type: ignore[no-untyped-def]
    return one(c, "SELECT kb.draft_product(%s, 'owner', 'desc', 'INTERNAL', 'daily', %s)", product, Jsonb(spec))


def onboard(as_api, product, spec=SPEC):  # type: ignore[no-untyped-def]
    with as_api("data.maker") as c:
        req = one(c, "SELECT kb.submit_product(%s, NULL)", draft(c, product, spec))
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'ok')", (req,))
    return req


def current(c, product):  # type: ignore[no-untyped-def]
    return c.execute("SELECT * FROM kb.data_policy_current WHERE product_id = %s", (product,)).fetchone()


def version(c) -> int:  # type: ignore[no-untyped-def]
    return int(one(c, "SELECT kb.policy_version()"))


# ------------------------------------------------------------------------------------------- P1 P4 happy path
def test_maker_drafts_checker_approves_and_the_policy_becomes_current(as_api):
    p = pid()
    with as_api("data.maker") as c:
        v0 = version(c)
        rev = draft(c, p)
        assert current(c, p) is None  # a draft is not policy
        req = one(c, "SELECT kb.submit_product(%s, 'please')", rev)
        assert current(c, p) is None  # neither is a submitted revision
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'ok')", (req,))
        row = current(c, p)
        assert row["spec"] == SPEC and version(c) == v0 + 1
        assert one(c, "SELECT status FROM kb.data_product WHERE product_id = %s", p) == "ACTIVE"


def test_the_maker_cannot_approve_their_own_request(as_api):
    p = pid()
    with as_api("data.maker") as c:
        req = one(c, "SELECT kb.submit_product(%s, NULL)", draft(c, p))
    with pytest.raises(pg.InsufficientPrivilege), as_api("data.maker") as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'self')", (req,))


@pytest.mark.parametrize("actor", ["ret.maker", "ret.checker", "data.checker", "ret.user1", "nobody"])
def test_only_data_domain_makers_draft(as_api, actor):
    with pytest.raises(pg.InsufficientPrivilege), as_api(actor) as c:
        draft(c, pid())


@pytest.mark.parametrize("actor", ["ret.checker", "data.maker", "ret.user1"])
def test_only_data_domain_checkers_decide(as_api, actor):
    p = pid()
    with as_api("data.maker") as c:
        req = one(c, "SELECT kb.submit_product(%s, NULL)", draft(c, p))
    with pytest.raises(pg.InsufficientPrivilege), as_api(actor) as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'x')", (req,))


def test_a_rejection_needs_a_reason_and_leaves_no_policy(as_api):
    p = pid()
    with as_api("data.maker") as c:
        req = one(c, "SELECT kb.submit_product(%s, NULL)", draft(c, p))
    with pytest.raises(pg.CheckViolation), as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, false, NULL)", (req,))
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, false, 'too wide')", (req,))
        assert current(c, p) is None


def test_a_decided_request_is_final(as_api):
    p = pid()
    req = onboard(as_api, p)
    with pytest.raises(pg.InsufficientPrivilege, match="final"), as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, false, 'changed my mind')", (req,))


# ------------------------------------------------------------------------------------------- P2 P3 direct DML
def test_a_submitted_revision_is_locked_and_tables_change_only_through_the_functions(as_api, as_admin):
    p = pid()
    with as_api("data.maker") as c:
        rev = draft(c, p)
        one(c, "SELECT kb.submit_product(%s, NULL)", rev)
    for sql in (
        "UPDATE kb.product_revision SET spec = '{}'::jsonb WHERE revision_id = %s",
        "DELETE FROM kb.product_revision WHERE revision_id = %s",
    ):
        with pytest.raises(pg.InsufficientPrivilege), as_api("data.maker") as c:
            c.execute(sql, (rev,))  # no table privilege for the app role
        with pytest.raises(pg.InsufficientPrivilege), as_admin() as c:
            c.execute(sql, (rev,))  # and the triggers stop even a superuser
    with pytest.raises(pg.CheckViolation), as_admin() as c:  # invariant: ACTIVE needs an approved revision
        c.execute("UPDATE kb.data_product SET status = 'ACTIVE' WHERE product_id = %s", (p,))
    with pytest.raises(pg.InsufficientPrivilege, match="data-product functions"), as_admin() as c:
        c.execute("UPDATE kb.data_product SET gov_bu = 'retail' WHERE product_id = %s", (p,))  # owner-only


def test_the_pipeline_cannot_govern_products(as_pipe, as_api):
    p = pid()
    with pytest.raises(pg.InsufficientPrivilege), as_pipe() as c:
        c.execute("SELECT kb.draft_product(%s, 'o', 'd', 'INTERNAL', 'r', %s)", (p, Jsonb(SPEC)))
    with as_api("data.maker") as c:
        req = one(c, "SELECT kb.submit_product(%s, NULL)", draft(c, p))
    with pytest.raises(pg.InsufficientPrivilege), as_pipe() as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'x')", (req,))


@pytest.mark.parametrize(
    "bad",
    [
        {"grants": ["GRP_A"], "row_filters": [], "column_masks": {"email": {"mask": "plaintext"}}},
        {"grants": ["not a group"], "row_filters": [], "column_masks": {}},
        {"grants": ["GRP_A"], "row_filters": [{"groups": [], "filter": "x = 1"}], "column_masks": {}},
        {"grants": ["GRP_A"], "row_filters": [], "column_masks": {}, "extra": 1},
        {"grants": ["GRP_A"], "row_filters": [], "column_masks": {"Bad Col": {"mask": "redact"}}},
    ],
)
def test_malformed_specs_are_refused_by_the_database(as_api, bad):
    with pytest.raises(pg.CheckViolation), as_api("data.maker") as c:
        draft(c, pid(), bad)


def test_a_deny_all_revision_cannot_be_submitted(as_api):
    with pytest.raises(pg.CheckViolation, match="at least one group"), as_api("data.maker") as c:
        one(
            c,
            "SELECT kb.submit_product(%s, NULL)",
            draft(c, pid(), {"grants": [], "row_filters": [], "column_masks": {}}),
        )


# ------------------------------------------------------------------------------------------- P5 restrictions
@pytest.mark.parametrize("actor", ["data.maker", "data.checker"])
def test_revoking_a_group_is_single_actor_and_removes_it_everywhere(as_api, actor):
    p = pid()
    onboard(as_api, p)
    with as_api(actor) as c:
        v0 = version(c)
        c.execute("SELECT kb.restrict_product(%s, 'REVOKE_GRANT', 'leaver', 'GRP_DATA_B', NULL, 'redact')", (p,))
        spec = current(c, p)["spec"]
        req = c.execute(
            "SELECT status, checker_id FROM kb.product_request WHERE product_id = %s AND request_type = %s",
            (p, "RESTRICT_PRODUCT"),
        ).fetchone()
        assert version(c) == v0 + 1
    assert spec["grants"] == ["GRP_DATA_A"]
    assert all("GRP_DATA_B" not in f["groups"] for f in spec["row_filters"]) and len(spec["row_filters"]) == 2
    assert spec["column_masks"]["email"] == {"mask": "redact", "unless_groups": []}
    assert req["status"] == "EXECUTED" and req["checker_id"] is None


def test_a_row_filter_left_without_groups_is_dropped(as_api):
    p = pid()
    onboard(as_api, p)
    with as_api("data.maker") as c:
        c.execute("SELECT kb.restrict_product(%s, 'REVOKE_GRANT', 'r', 'GRP_DATA_A', NULL, 'redact')", (p,))
        spec = current(c, p)["spec"]
    assert spec["grants"] == ["GRP_DATA_B"]
    assert spec["row_filters"] == [{"groups": ["GRP_DATA_B"], "filter": "bu = 'corporate'"}]


def test_adding_a_mask_masks_and_never_changes_an_existing_mask_type(as_api):
    p = pid()
    onboard(as_api, p)
    with as_api("data.maker") as c:
        c.execute("SELECT kb.restrict_product(%s, 'ADD_MASK', 'pii', NULL, 'phone', 'hash')", (p,))
        c.execute("SELECT kb.restrict_product(%s, 'ADD_MASK', 'pii', NULL, 'email', 'null')", (p,))
        masks = current(c, p)["spec"]["column_masks"]
    assert masks["phone"] == {"mask": "hash", "unless_groups": []}
    assert masks["email"] == {"mask": "redact", "unless_groups": []}  # kept redact; exemption removed


@pytest.mark.parametrize("actor", ["ret.maker", "ret.user1", "nobody"])
def test_outsiders_cannot_restrict(as_api, actor):
    p = pid()
    onboard(as_api, p)
    with pytest.raises(pg.InsufficientPrivilege), as_api(actor) as c:
        c.execute("SELECT kb.restrict_product(%s, 'SUSPEND', 'x', NULL, NULL, 'redact')", (p,))


def test_suspend_denies_at_once_and_reactivation_needs_a_checker(as_api):
    p = pid()
    onboard(as_api, p)
    with as_api("data.maker") as c:
        c.execute("SELECT kb.restrict_product(%s, 'SUSPEND', 'incident', NULL, NULL, 'redact')", (p,))
        assert current(c, p) is None
        req = one(c, "SELECT kb.request_product_reactivation(%s, 'fixed')", p)
        assert current(c, p) is None  # requesting is not reactivating
    with pytest.raises(pg.InsufficientPrivilege), as_api("data.maker") as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'self')", (req,))
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'ok')", (req,))
        assert current(c, p)["spec"] == SPEC


def test_a_restriction_needs_a_reason(as_api):
    p = pid()
    onboard(as_api, p)
    with pytest.raises(pg.RaiseException, match="reason"), as_api("data.maker") as c:
        c.execute("SELECT kb.restrict_product(%s, 'SUSPEND', '', NULL, NULL, 'redact')", (p,))


# ------------------------------------------------------------------------------------------- P6
def test_a_change_drafted_before_a_restriction_cannot_be_approved_after_it(as_api):
    p = pid()
    onboard(as_api, p)
    with as_api("data.maker") as c:
        wider = dict(SPEC, grants=["GRP_DATA_A", "GRP_DATA_B", "GRP_DATA_C"])
        req = one(c, "SELECT kb.submit_product(%s, 'add C')", draft(c, p, wider))
    with as_api("data.checker") as c:  # meanwhile B is revoked
        c.execute("SELECT kb.restrict_product(%s, 'REVOKE_GRANT', 'leaver', 'GRP_DATA_B', NULL, 'redact')", (p,))
    with pytest.raises(pg.SerializationFailure, match="redraft"), as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'ok')", (req,))
    with as_api("data.checker") as c:
        assert "GRP_DATA_B" not in current(c, p)["spec"]["grants"]


# ------------------------------------------------------------------------------------------- P7
def test_every_step_is_audited(as_api):
    p = pid()
    onboard(as_api, p)
    with as_api("data.maker") as c:
        c.execute("SELECT kb.restrict_product(%s, 'ADD_MASK', 'pii', NULL, 'phone', 'hash')", (p,))
        rows = c.execute(
            """SELECT actor_id, object_type FROM kb.audit_log
                WHERE object_id = %s OR (object_type IN ('product_revision', 'product_request')
                      AND after_state->>'product_id' = %s)""",
            (p, p),
        ).fetchall()
    kinds = {(r["actor_id"], r["object_type"]) for r in rows}
    assert ("data.maker", "data_product") in kinds and ("data.checker", "product_request") in kinds
    assert ("data.maker", "product_revision") in kinds
