"""FsspecRawStore: content-addressed-safe raw store over any fsspec URL (file:// locally, hdfs:// on CML)."""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, BinaryIO

import fsspec

from kb.config import REPO_ROOT
from kb.ports.types import StoredObject

CHUNK = 1024 * 1024


class FsspecRawStore:
    STATUS = "implemented"
    PHASE = 0

    def __init__(self, url: str, **_: Any) -> None:
        if url.startswith("file://") and not url.startswith("file:///"):
            rel = url.removeprefix("file://")
            url = (REPO_ROOT / rel).resolve().as_uri()
        self.fs, self.root = fsspec.core.url_to_fs(url)
        self.fs.makedirs(self.root, exist_ok=True)
        self.protocol = self.fs.protocol if isinstance(self.fs.protocol, str) else self.fs.protocol[0]

    def _path(self, key: str) -> str:
        norm = posixpath.normpath(key.replace("\\", "/"))
        if norm.startswith(("../", "/")) or norm == "..":
            raise ValueError(f"key escapes the raw store: {key!r}")
        return posixpath.join(self.root, norm)

    def _uri(self, path: str) -> str:
        return f"{self.protocol}://{path}" if self.protocol != "file" else f"file:///{path.lstrip('/')}"

    def put_stream(self, key: str, stream: BinaryIO) -> StoredObject:
        """Stream to a temp object, hash on the fly, then move into place (never whole-file in memory)."""
        dest = self._path(key)
        tmp = dest + ".partial"
        self.fs.makedirs(posixpath.dirname(dest), exist_ok=True)
        h = hashlib.sha256()
        size = 0
        try:
            with self.fs.open(tmp, "wb") as out:
                while block := stream.read(CHUNK):
                    h.update(block)
                    size += len(block)
                    out.write(block)
        except BaseException:
            if self.fs.exists(tmp):  # aborted (e.g. size limit hit mid-stream): leave nothing behind
                self.fs.rm(tmp)
            raise
        self.fs.mv(tmp, dest)
        return StoredObject(uri=self._uri(dest), sha256=h.hexdigest(), size_bytes=size)

    def _strip(self, uri: str) -> str:
        _, path = fsspec.core.url_to_fs(uri)
        if not str(path).replace("\\", "/").startswith(str(self.root).replace("\\", "/")):
            raise ValueError(f"uri outside raw store: {uri}")
        return str(path)

    @contextmanager
    def open(self, uri: str) -> Iterator[BinaryIO]:
        with self.fs.open(self._strip(uri), "rb") as f:
            yield f

    def exists(self, uri: str) -> bool:
        return bool(self.fs.exists(self._strip(uri)))

    def delete(self, uri: str) -> None:
        self.fs.rm(self._strip(uri))
