from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kb.config import REPO_ROOT, Settings, load_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings("local")


@pytest.fixture(scope="session")
def data_dir(settings: Settings) -> Path:
    return settings.path("data_dir")


@pytest.fixture(scope="session")
def manifest(data_dir: Path) -> dict[str, Any]:
    p = data_dir / "synthetic_manifest.json"
    if not p.exists():
        pytest.fail("synthetic world missing: run `make seed` (or .\\kb.ps1 seed) first")
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


class InternetBlockedError(OSError):
    pass


@pytest.fixture()
def no_internet(monkeypatch):
    """Refuse every non-loopback socket connection for the duration of a test (rule 2 evidence)."""
    import ipaddress
    import socket

    real_connect = socket.socket.connect

    def guarded(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        try:
            ok = ipaddress.ip_address(host).is_loopback
        except ValueError:
            ok = host in ("localhost",)
        if not ok:
            raise InternetBlockedError(f"blocked outbound connection to {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", _loopback_only_getaddrinfo(socket.getaddrinfo))


def _loopback_only_getaddrinfo(real):  # type: ignore[no-untyped-def]
    def wrapper(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            raise InternetBlockedError(f"blocked DNS lookup for {host!r}")
        return real(host, *args, **kwargs)

    return wrapper
