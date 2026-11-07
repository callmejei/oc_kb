"""LocalFolderSource reproduces Graph delta semantics over a copy of the synthetic tenant."""

from __future__ import annotations

import shutil

import pytest

from kb.adapters.local.folder_source import InvalidTokenError, LocalFolderSource
from kb.ports.types import ChangeKind
from synthetic import scenarios
from synthetic.world import CATALOGUE


@pytest.fixture()
def sp(tmp_path, data_dir):
    root = tmp_path / "sharepoint"
    shutil.copytree(data_dir / "sharepoint", root)
    return root


def run_delta(src, token):
    pages = list(src.delta(token))
    assert all(p.next_token is None for p in pages[:-1]), "only the last page carries the token"
    return [i for p in pages for i in p.items], pages[-1].next_token, pages


def test_initial_delta_lists_live_in_scope_items_and_pages(sp):
    src = LocalFolderSource(sp, page_size=4).bind("retail", "drive", "/KB")
    items, token, pages = run_delta(src, None)
    expected = {d.filename for d in CATALOGUE if d.bu == "retail"}
    assert {i.name for i in items} == expected
    assert len(pages) == -(-len(expected) // 4) and token and token.startswith("seq:")
    assert all(i.change is ChangeKind.UPSERT and i.sha256 and i.ctag for i in items)


def test_no_changes_means_empty_delta_and_same_token(sp):
    src = LocalFolderSource(sp).bind("retail", "d", "/KB")
    _, token, _ = run_delta(src, None)
    items, token2, _ = run_delta(src, token)
    assert items == [] and token2 == token


def test_edit_appears_with_same_id_and_new_ctag(sp):
    src = LocalFolderSource(sp).bind("retail", "d", "/KB")
    first, token, _ = run_delta(src, None)
    out = scenarios.new_version(sp, "ret-home-loan-policy")
    items, _, _ = run_delta(src, token)
    (item,) = items
    before = next(i for i in first if i.external_item_id == out["item_id"])
    assert item.change is ChangeKind.UPSERT and item.ctag != before.ctag and item.sha256 != before.sha256


def test_delete_carries_deleted_facet(sp):
    src = LocalFolderSource(sp).bind("corporate", "d", "/KB")
    _, token, _ = run_delta(src, None)
    out = scenarios.delete(sp, "corp-trade-proc")
    (item,) = run_delta(src, token)[0]
    assert item.change is ChangeKind.DELETED and item.external_item_id == out["item_id"]


def test_move_out_of_scope_is_reported_with_stable_id(sp):
    src = LocalFolderSource(sp).bind("wealth", "d", "/KB")
    _, token, _ = run_delta(src, None)
    out = scenarios.move_out_of_scope(sp, "wlth-fees")
    (item,) = run_delta(src, token)[0]
    assert item.change is ChangeKind.OUT_OF_SCOPE and item.external_item_id == out["item_id"]
    assert item.path.startswith("Archive/")
    assert out["item_id"] not in {i.external_item_id for i in src.list_all()}


def test_other_sites_changes_are_invisible(sp):
    src = LocalFolderSource(sp).bind("retail", "d", "/KB")
    _, token, _ = run_delta(src, None)
    scenarios.delete(sp, "corp-trade-proc")
    assert run_delta(src, token)[0] == []


def test_expired_token_requires_resync(sp):
    src = LocalFolderSource(sp).bind("retail", "d", "/KB")
    _, token, _ = run_delta(src, None)
    scenarios.new_version(sp, "ret-fees")
    scenarios.expire_tokens(sp)
    pages = list(src.delta(token))
    assert len(pages) == 1 and pages[0].resync_required and pages[0].next_token is None
    fresh, new_token, _ = run_delta(src, None)
    assert new_token != token and len(fresh) == len(list(src.list_all()))
    assert run_delta(src, new_token)[0] == []


def test_invalid_token_and_path_escape(sp):
    src = LocalFolderSource(sp).bind("retail", "d", "/KB")
    with pytest.raises(InvalidTokenError):
        list(src.delta("garbage"))
    with pytest.raises(ValueError):
        LocalFolderSource(sp).bind("retail", "d", "/KB/../../etc")


def test_open_streams_the_file(sp):
    src = LocalFolderSource(sp).bind("shared", "d", "/KB")
    item = next(iter(src.list_all()))
    with src.open(item) as f:
        assert len(f.read()) == item.size_bytes
