"""ElasticsearchChunkIndex: pre-filtering, version switch, ACL re-stamp. Uses a throwaway alias."""

from __future__ import annotations

import os
import uuid

import pytest

from kb.adapters.local.es_index import ElasticsearchChunkIndex, bm25_body, knn_body, render_filter
from kb.config import REPO_ROOT, read_dotenv
from kb.ports.types import AccessFilter, Chunk

pytestmark = pytest.mark.integration
DIMS = 4


@pytest.fixture()
def index():
    env = {**read_dotenv(REPO_ROOT / ".env.local"), **os.environ}
    idx = ElasticsearchChunkIndex(
        "http://127.0.0.1:9250", f"kb-test-{uuid.uuid4().hex[:8]}", DIMS, password=env["KB_ES_PASSWORD"]
    )
    idx.ensure_index()
    yield idx
    idx.drop()


def chunk(cid, doc, ver, text, acl, vec, current=True, bu="retail", source="s1"):
    return Chunk(
        cid,
        doc,
        ver,
        text,
        vec,
        acl,
        bu,
        current,
        True,
        page=1,
        section_path="1. Purpose",
        metadata={"source_id": source, "version_no": int(ver[-1]), "title": doc},
    )


def ids(hits):
    return sorted(h.chunk.chunk_id for h in hits)


RETAIL = AccessFilter(frozenset({"GRP_KB_RETAIL_READ", "user:ret.user1"}))
WEALTH = AccessFilter(frozenset({"GRP_KB_WEALTH_READ", "user:wlth.user1"}))
NOBODY = AccessFilter(frozenset())


def test_filter_is_inside_both_queries_never_post():
    b = bm25_body("ltv", RETAIL, 5)
    k = knn_body([0.0] * DIMS, RETAIL, 5)
    assert b["query"]["bool"]["filter"] == render_filter(RETAIL)
    assert k["knn"]["filter"] == render_filter(RETAIL)
    assert "post_filter" not in b and "post_filter" not in k
    assert {"terms": {"acl_groups": ["GRP_KB_RETAIL_READ", "user:ret.user1"]}} in render_filter(RETAIL)
    assert {"term": {"is_current": True}} in render_filter(RETAIL)
    assert {"term": {"source_active": True}} in render_filter(RETAIL)


def test_empty_principals_match_nothing():
    assert render_filter(NOBODY) == [{"match_none": {}}]


def test_acl_prefilter_in_bm25_and_knn(index):
    index.upsert(
        [
            chunk("r1", "dR", "dR:v1", "retail loan to value limit", ["GRP_KB_RETAIL_READ"], [1, 0, 0, 0]),
            chunk("w1", "dW", "dW:v1", "wealth loan to value limit", ["GRP_KB_WEALTH_READ"], [1, 0, 0, 0], bu="wealth"),
            chunk("p1", "dP", "dP:v1", "private loan to value notes", ["user:ret.user1"], [1, 0, 0, 0]),
        ]
    )
    assert ids(index.bm25("loan to value", RETAIL, 10)) == ["p1", "r1"]
    assert ids(index.bm25("loan to value", WEALTH, 10)) == ["w1"]
    assert ids(index.knn([1, 0, 0, 0], RETAIL, 10)) == ["p1", "r1"]
    assert ids(index.knn([1, 0, 0, 0], WEALTH, 10)) == ["w1"]
    assert index.bm25("loan", NOBODY, 10) == [] and index.knn([1, 0, 0, 0], NOBODY, 10) == []


def test_version_switch_is_one_request_and_hides_old(index):
    index.upsert([chunk("a1", "d1", "d1:v1", "ltv eighty percent", ["GRP_KB_RETAIL_READ"], [1, 0, 0, 0])])
    index.upsert(
        [chunk("b1", "d1", "d1:v2", "ltv seventy five percent", ["GRP_KB_RETAIL_READ"], [1, 0, 0, 0], current=False)]
    )
    assert ids(index.bm25("ltv", RETAIL, 10)) == ["a1"]
    index.set_visibility("d1", current_version_id="d1:v2")
    assert ids(index.bm25("ltv", RETAIL, 10)) == ["b1"]
    index.set_visibility("d1", current_version_id=None)  # deletion: nothing visible
    assert index.bm25("ltv", RETAIL, 10) == [] and index.count({"term": {"doc_id": "d1"}}) == 2


def test_restamp_acl_and_source_active(index):
    index.upsert([chunk("u1", "dU", "dU:v1", "uploaded branch tips", ["user:ret.user1"], [0, 1, 0, 0])])
    other = AccessFilter(frozenset({"GRP_KB_RETAIL_READ", "user:ret.user2"}))
    assert index.bm25("branch tips", other, 10) == []
    index.restamp_acl("dU", ["GRP_KB_RETAIL_READ"])  # publish: no re-embedding
    assert ids(index.bm25("branch tips", other, 10)) == ["u1"]
    index.set_source_active("s1", False)
    assert index.bm25("branch tips", other, 10) == []
    with pytest.raises(ValueError):
        index.restamp_acl("dU", [])


def test_delete_version(index):
    index.upsert([chunk("x1", "dX", "dX:v1", "purge me", ["GRP_KB_RETAIL_READ"], [0, 0, 1, 0])])
    index.delete_version("dX:v1")
    assert index.count({"term": {"doc_id": "dX"}}) == 0


def test_strict_mapping_rejects_unknown_fields(index):
    bad = Chunk("z", "dZ", "dZ:v1", "t", [0, 0, 0, 1], ["g"], "retail", True, True, metadata={"surprise": 1})
    from elasticsearch.helpers import BulkIndexError

    with pytest.raises(BulkIndexError) as e:
        index.upsert([bad])
    assert e.value.errors[0]["index"]["error"]["type"] == "strict_dynamic_mapping_exception"
