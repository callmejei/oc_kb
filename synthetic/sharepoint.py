"""Simulated SharePoint server state for the synthetic tenant.

Files live under ``data/sharepoint/<bu>/...``. ``_ledger.json`` is the "server side" that
Graph would hold: stable item ids (survive moves), cTag/eTag, a deleted facet and a monotonically
increasing change sequence so ``LocalFolderSource`` (Phase 2) can serve delta pages and tokens.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

LEDGER = "_ledger.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


@dataclass
class LedgerItem:
    item_id: str
    path: str  # relative to the sharepoint root, posix
    sha256: str
    size_bytes: int
    version: int
    ctag: str
    etag: str
    changed_seq: int
    deleted: bool = False


class SharePointSim:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / LEDGER
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.seq: int = raw["seq"]
            self.items = {k: LedgerItem(**v) for k, v in raw["items"].items()}
        else:
            self.seq, self.items = 0, {}

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"seq": self.seq, "items": {k: asdict(v) for k, v in sorted(self.items.items())}}
        self.path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def item_id_for(seed: str) -> str:
        return "01SYN" + hashlib.sha1(seed.encode(), usedforsecurity=False).hexdigest()[:22].upper()

    def by_path(self, rel: str) -> LedgerItem | None:
        return next((i for i in self.items.values() if i.path == rel and not i.deleted), None)

    def _tick(self) -> int:
        self.seq += 1
        return self.seq

    # ------------------------------------------------------------------ operations
    def upsert(self, rel: str, seed: str | None = None) -> LedgerItem:
        """Register a file already written at ``rel`` (new item or new content version)."""
        f = self.root / rel
        digest, size = sha256_file(f), f.stat().st_size
        item = self.by_path(rel)
        if item is None:
            iid = self.item_id_for(seed or rel)
            prev = self.items.get(iid)
            version = (prev.version + 1) if prev else 1
            item = LedgerItem(iid, rel, digest, size, version, "", "", 0)
            self.items[iid] = item
        elif item.sha256 == digest:
            return item  # unchanged content: no new change event (cTag stable)
        else:
            item.version += 1
        item.sha256, item.size_bytes, item.deleted = digest, size, False
        item.ctag = f'"c:{{{item.item_id}}},{item.version}"'
        item.etag = f'"{{{item.item_id}}},{item.version}"'
        item.changed_seq = self._tick()
        return item

    def delete(self, rel: str) -> LedgerItem:
        item = self.by_path(rel)
        if item is None:
            raise FileNotFoundError(rel)
        (self.root / rel).unlink()
        item.deleted = True
        item.changed_seq = self._tick()
        return item

    def move(self, rel: str, new_rel: str) -> LedgerItem:
        item = self.by_path(rel)
        if item is None:
            raise FileNotFoundError(rel)
        dest = self.root / new_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.root / rel), str(dest))
        item.path = new_rel  # same item id: Graph ids survive moves
        item.etag = f'"{{{item.item_id}}},{item.version}m"'  # metadata change, cTag unchanged
        item.changed_seq = self._tick()
        return item
