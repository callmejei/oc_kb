"""Lifecycle scenarios mutate a copy of the tenant with Graph-like semantics."""

from __future__ import annotations

import shutil

import pytest

from synthetic import scenarios
from synthetic.sharepoint import SharePointSim
from synthetic.world import DOC_BY_KEY, SOURCE_MAX_FILE_BYTES


@pytest.fixture()
def sp(tmp_path, data_dir):
    root = tmp_path / "sharepoint"
    shutil.copytree(data_dir / "sharepoint", root)
    return root


def test_new_version_keeps_item_id_and_changes_ctag(sp):
    key = "ret-home-loan-policy"
    before = SharePointSim(sp).items[SharePointSim.item_id_for(key)]
    out = scenarios.new_version(sp, key)
    after = SharePointSim(sp).items[out["item_id"]]
    assert out["item_id"] == before.item_id and after.version == 2
    assert after.ctag != before.ctag and after.sha256 != before.sha256
    assert after.changed_seq > before.changed_seq
    assert after.path == before.path


def test_delete_sets_deleted_facet(sp):
    out = scenarios.delete(sp, "corp-trade-proc")
    item = SharePointSim(sp).items[out["item_id"]]
    assert item.deleted and not (sp / item.path).exists()


def test_move_out_of_scope_keeps_id_and_ctag(sp):
    key = "wlth-fees"
    before = SharePointSim(sp).items[SharePointSim.item_id_for(key)]
    out = scenarios.move_out_of_scope(sp, key)
    after = SharePointSim(sp).items[out["item_id"]]
    assert after.item_id == before.item_id and after.ctag == before.ctag
    assert after.path.startswith("wealth/Archive/") and (sp / after.path).exists()


def test_oversized_unsupported_corrupt(sp):
    big = scenarios.add_oversized(sp, "retail")
    assert big["size_bytes"] > SOURCE_MAX_FILE_BYTES
    unsup = scenarios.add_unsupported(sp, "retail")
    assert unsup["path"].endswith(".mpp")
    bad = scenarios.add_corrupt(sp, "retail")
    assert (sp / bad["path"]).read_bytes().startswith(b"%PDF")


def test_unchanged_rewrite_is_not_a_change(sp):
    sim = SharePointSim(sp)
    spec = DOC_BY_KEY["shr-code-of-conduct"]
    seq = sim.seq
    sim.upsert(spec.rel_path)
    assert sim.seq == seq
