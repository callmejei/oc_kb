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
