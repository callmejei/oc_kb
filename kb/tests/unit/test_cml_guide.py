"""Phase 7 acceptance: docs/cml-deployment.md covers every port, names every CML adapter, and keeps the
security sections (secrets, Ray Serve / vLLM, Starburst impersonation, network rules, Ray dashboard)."""

from __future__ import annotations

import re

import pytest

from kb.config import PORT_NAMES, REPO_ROOT, load_settings

GUIDE = (REPO_ROOT / "docs" / "cml-deployment.md").read_text(encoding="utf-8")
HEADINGS = set(re.findall(r"^### (\S+)\s*$", GUIDE, re.M))


@pytest.mark.parametrize("port", PORT_NAMES)
def test_every_port_has_its_own_section(port):
    assert port in HEADINGS, f"docs/cml-deployment.md has no '### {port}' section"


def test_no_section_for_a_port_that_does_not_exist():
    assert set(PORT_NAMES) >= HEADINGS, HEADINGS - set(PORT_NAMES)


@pytest.mark.parametrize("port", PORT_NAMES)
def test_the_configured_cml_adapter_is_named(port):
    adapter = load_settings("cml.example").ports[port].adapter
    assert f"`{adapter}`" in GUIDE, f"{port}: {adapter} not mentioned"


@pytest.mark.parametrize(
    "topic",
    ["## 3. Secrets", "## 4. Ray Serve and vLLM endpoints", "## 5. Starburst impersonation", "## 6. Network rules",
     "Never expose the Ray dashboard", "--dashboard-host=127.0.0.1", "X-Trino-User", "allowed_hosts"],
)  # fmt: skip
def test_required_topics_are_covered(topic):
    assert topic in GUIDE


def test_every_secret_referenced_by_the_cml_config_is_documented():
    raw = (REPO_ROOT / "config" / "cml.example.yaml").read_text(encoding="utf-8")
    for var in set(re.findall(r"\$\{([A-Z0-9_]+)\}", raw)):
        assert f"`{var}`" in GUIDE or var in GUIDE, var
