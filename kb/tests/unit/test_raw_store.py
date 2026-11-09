from __future__ import annotations

import hashlib
import io

import pytest

from kb.adapters.local.raw_store import FsspecRawStore


def test_put_stream_hashes_and_round_trips(tmp_path):
    store = FsspecRawStore(tmp_path.as_uri())
    payload = b"x" * (3 * 1024 * 1024 + 17)  # spans several stream blocks
    obj = store.put_stream("retail/doc/v1.bin", io.BytesIO(payload))
    assert obj.sha256 == hashlib.sha256(payload).hexdigest()
    assert obj.size_bytes == len(payload)
    assert store.exists(obj.uri)
    with store.open(obj.uri) as f:
        assert f.read() == payload
    store.delete(obj.uri)
    assert not store.exists(obj.uri)


@pytest.mark.parametrize("key", ["../escape.bin", "/abs.bin", "a/../../b.bin"])
def test_keys_cannot_escape_the_store(tmp_path, key):
    store = FsspecRawStore((tmp_path / "raw").as_uri())
    with pytest.raises(ValueError):
        store.put_stream(key, io.BytesIO(b"x"))


def test_uris_outside_store_are_refused(tmp_path):
    store = FsspecRawStore((tmp_path / "raw").as_uri())
    (tmp_path / "other.bin").write_bytes(b"x")
    with pytest.raises(ValueError):
        store.exists((tmp_path / "other.bin").as_uri())
