"""Migration 012 (D-067): metrics are governed in the database. Four-eyes, pipeline excluded, no masked columns (at
submit and at approval), metrics only on ACTIVE products, stale approval refused, single-actor retire.
Runs on kb_test with unique ids per test."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from psycopg.types.json import Jsonb

SPEC = {
    "grants": ["GRP_DATA_A"],
    "row_filters": [{"groups": ["GRP_DATA_A"], "filter": "bu = 'x'"}],
    "column_masks": {"email": {"mask": "redact", "unless_groups": []}},
}
DRAFT = "SELECT kb.draft_metric(%s, %s, 'd')"
SUBMIT = "SELECT kb.submit_metric(%s, NULL)"


def one(c, sql, *p):  # type: ignore[no-untyped-def]
    return next(iter(c.execute(sql, p).fetchone().values()))


@pytest.fixture()
def product(as_api):  # type: ignore[no-untyped-def]
    pid = f"mart_t.m{uuid.uuid4().hex[:10]}"
    with as_api("data.maker") as c:
        rev = one(c, "SELECT kb.draft_product(%s, 'o', 'd', 'INTERNAL', 'r', %s)", pid, Jsonb(SPEC))
        req = one(c, "SELECT kb.submit_product(%s, NULL)", rev)
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_product_request(%s, true, 'ok')", (req,))
    return pid


def defn(table: str, column: str = "amount") -> dict:  # type: ignore[type-arg]
    return {
        "label": "Total amount",
        "synonyms": [],
        "concept": "",
        "table": table,
        "measure": {"agg": "sum", "column": column},
        "dimensions": [],
        "unit": "number",
        "columns": [column],
    }


def mid() -> str:
    return f"m_{uuid.uuid4().hex[:10]}"


def submit(c, m, d):  # type: ignore[no-untyped-def]
    return one(c, SUBMIT, one(c, DRAFT, m, Jsonb(d)))


def publish(as_api, m, d):  # type: ignore[no-untyped-def]
    with as_api("data.maker") as c:
        req = submit(c, m, d)
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_metric_request(%s, true, 'ok')", (req,))


def published(c, m) -> bool:  # type: ignore[no-untyped-def]
    return c.execute("SELECT 1 FROM kb.metric_current WHERE metric_id = %s", (m,)).fetchone() is not None


def test_publish_needs_a_checker_and_the_maker_cannot_self_approve(as_api, product):
    m = mid()
    with as_api("data.maker") as c:
        req = submit(c, m, defn(product))
        assert not published(c, m)
    with pytest.raises(pg.InsufficientPrivilege), as_api("data.maker") as c:
        c.execute("SELECT kb.decide_metric_request(%s, true, 'self')", (req,))
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_metric_request(%s, true, 'ok')", (req,))
        assert published(c, m)


def test_a_metric_on_a_masked_column_is_refused_at_submit(as_api, product):
    with pytest.raises(pg.CheckViolation, match="masked columns: email"), as_api("data.maker") as c:
        submit(c, mid(), defn(product, "email"))


def test_a_mask_added_while_pending_blocks_approval(as_api, product):
    with as_api("data.maker") as c:
        req = submit(c, mid(), defn(product, "phone"))
        c.execute("SELECT kb.restrict_product(%s, 'ADD_MASK', 'pii', NULL, 'phone', 'hash')", (product,))
    with pytest.raises(pg.CheckViolation, match="masked columns: phone"), as_api("data.checker") as c:
        c.execute("SELECT kb.decide_metric_request(%s, true, 'ok')", (req,))


def test_metrics_only_on_active_products_and_only_by_data_makers(as_api, product):
    with pytest.raises(pg.CheckViolation, match="not an ACTIVE data product"), as_api("data.maker") as c:
        c.execute(DRAFT, (mid(), Jsonb(defn("mart_t.nope"))))
    for actor in ("ret.maker", "data.checker", "ret.user1"):
        with pytest.raises(pg.InsufficientPrivilege), as_api(actor) as c:
            c.execute(DRAFT, (mid(), Jsonb(defn(product))))


def test_retire_is_single_actor_and_suspending_the_product_hides_its_metrics(as_api, product):
    m1, m2 = mid(), mid()
    publish(as_api, m1, defn(product))
    publish(as_api, m2, defn(product))
    with as_api("data.checker") as c:
        c.execute("SELECT kb.retire_metric(%s, 'superseded')", (m1,))
        assert not published(c, m1) and published(c, m2)
        c.execute("SELECT kb.restrict_product(%s, 'SUSPEND', 'incident', NULL, NULL, 'redact')", (product,))
        assert not published(c, m2)  # the view requires an ACTIVE product


def test_a_revision_drafted_before_another_approval_is_stale(as_api, product):
    m = mid()
    publish(as_api, m, defn(product))  # revision 1 current
    with as_api("data.maker") as c:
        r2 = submit(c, m, defn(product, "total"))  # revision 2, drafted against 1, pending
        v3 = one(c, DRAFT, m, Jsonb(defn(product, "net")))  # revision 3, also drafted against 1
    with as_api("data.checker") as c:
        c.execute("SELECT kb.decide_metric_request(%s, true, 'ok')", (r2,))  # 2 becomes current
    with as_api("data.maker") as c:
        r3 = one(c, SUBMIT, v3)
    with pytest.raises(pg.SerializationFailure, match="redraft"), as_api("data.checker") as c:
        c.execute("SELECT kb.decide_metric_request(%s, true, 'ok')", (r3,))


def test_the_pipeline_cannot_govern_metrics(as_pipe, product):
    with pytest.raises(pg.InsufficientPrivilege), as_pipe() as c:
        c.execute(DRAFT, (mid(), Jsonb(defn(product))))
