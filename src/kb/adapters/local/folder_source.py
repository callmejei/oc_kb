"""LocalFolderSource: SharePoint/Graph delta semantics over the synthetic tenant.

The folder ``<root>/<site>/...`` holds the files; ``<root>/_ledger.json`` is the simulated server
state (written by ``synthetic``): stable item ids that survive moves, cTag/eTag, a deleted facet,
a change sequence and a token floor (tokens below it are expired, like Graph's 410 Gone).

Mirrors Graph ``/drives/{id}/root/delta``:
- ``delta(None)`` returns every live item of the drive (site), then a token;
- ``delta(token)`` returns every item of the drive changed since the token, including deletions
  and items moved elsewhere. Classification against the onboarded folder happens here:
  inside the folder -> UPSERT, outside -> OUT_OF_SCOPE, deleted facet -> DELETED;
- only the last page carries ``next_token``; an expired token yields one page with
  ``resync_required=True``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from kb.config import REPO_ROOT
from kb.ports.types import ChangeKind, DeltaPage, SourceItem

LEDGER = "_ledger.json"
TOKEN_PREFIX = "seq:"  # noqa: S105 - a delta token prefix, not a credential


class InvalidTokenError(ValueError):
    pass


def _parse_token(token: str) -> int:
    if not token.startswith(TOKEN_PREFIX) or not token[len(TOKEN_PREFIX) :].isdigit():
        raise InvalidTokenError(f"not a delta token: {token!r}")
    return int(token[len(TOKEN_PREFIX) :])


@dataclass(frozen=True)
class BoundFolderSource:
    root: Path
    site: str
    folder: PurePosixPath  # relative to the site, e.g. "KB"
    page_size: int

    # ---------------------------------------------------------------- ledger
    def _ledger(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.root / LEDGER).read_text(encoding="utf-8"))
        return data

    def _site_items(self, ledger: dict[str, Any]) -> list[dict[str, Any]]:
        prefix = f"{self.site}/"
        items = [i for i in ledger["items"].values() if i["path"].startswith(prefix)]
        return sorted(items, key=lambda i: (i["changed_seq"], i["item_id"]))

    def _in_scope(self, site_rel: str) -> bool:
        if self.folder == PurePosixPath("."):  # whole drive onboarded
            return True
        return PurePosixPath(site_rel).is_relative_to(self.folder)

    def _to_item(self, raw: dict[str, Any], change: ChangeKind | None = None) -> SourceItem:
        site_rel = raw["path"][len(self.site) + 1 :]
        if change is None:
            if raw["deleted"]:
                change = ChangeKind.DELETED
            elif self._in_scope(site_rel):
                change = ChangeKind.UPSERT
            else:
                change = ChangeKind.OUT_OF_SCOPE
        f = self.root / raw["path"]
        mtime = f.stat().st_mtime if f.exists() else 0.0
        return SourceItem(
            external_item_id=raw["item_id"],
            name=PurePosixPath(site_rel).name,
            path=site_rel,
            size_bytes=raw["size_bytes"],
            ctag=raw["ctag"],
            etag=raw["etag"],
            modified_at=datetime.fromtimestamp(mtime, tz=UTC),
            change=change,
            sha256=raw["sha256"],
        )

    def _pages(self, items: list[SourceItem], token: str) -> Iterator[DeltaPage]:
        if not items:
            yield DeltaPage(items=[], next_token=token)
            return
        for start in range(0, len(items), self.page_size):
            chunk = items[start : start + self.page_size]
            last = start + self.page_size >= len(items)
            yield DeltaPage(items=chunk, next_token=token if last else None)

    # ---------------------------------------------------------------- port
    def delta(self, token: str | None) -> Iterator[DeltaPage]:
        ledger = self._ledger()
        now = f"{TOKEN_PREFIX}{ledger['seq']}"
        if token is None:
            live = [self._to_item(i) for i in self._site_items(ledger) if not i["deleted"]]
            yield from self._pages([i for i in live if i.change is ChangeKind.UPSERT], now)
            return
        since = _parse_token(token)
        if since < ledger.get("token_floor", 0):
            yield DeltaPage(items=[], next_token=None, resync_required=True)
            return
        changed = [self._to_item(i) for i in self._site_items(ledger) if i["changed_seq"] > since]
        yield from self._pages(changed, now)

    def list_all(self) -> Iterator[SourceItem]:
        """Current in-scope items (for reconcile / resync)."""
        for raw in self._site_items(self._ledger()):
            item = self._to_item(raw)
            if item.change is ChangeKind.UPSERT:
                yield item

    @contextmanager
    def open(self, item: SourceItem) -> Iterator[BinaryIO]:
        path = (self.root / self.site / item.path).resolve()
        if not path.is_relative_to((self.root / self.site).resolve()):
            raise ValueError(f"item path escapes the site: {item.path}")
        with open(path, "rb") as f:
            yield f


class LocalFolderSource:
    """Port factory: bind(site, drive, folder) -> a DocumentSource for one onboarded library."""

    STATUS = "implemented"
    PHASE = 2

    def __init__(self, root: str | Path, page_size: int = 25, **_: Any) -> None:
        p = Path(root)
        self.root = p if p.is_absolute() else REPO_ROOT / p
        self.page_size = page_size

    def bind(self, site_id: str, drive_id: str, folder_path: str) -> BoundFolderSource:
        folder = PurePosixPath(folder_path.strip("/") or ".")
        if ".." in folder.parts or "/" in site_id or "\\" in site_id:
            raise ValueError("invalid site or folder")
        return BoundFolderSource(self.root, site_id, folder, self.page_size)
