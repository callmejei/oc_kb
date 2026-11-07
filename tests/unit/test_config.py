from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kb.config import CONFIG_DIR, PORT_NAMES, ConfigError, load_settings


def test_local_config_loads_with_every_port(settings):
    assert settings.env == "local"
    assert set(settings.ports) == set(PORT_NAMES)


def test_cml_example_loads_and_lists_every_port():
    s = load_settings("cml.example")
    assert set(s.ports) == set(PORT_NAMES)
    assert s.ports["embedder"].options["dims"] == s.ports["chunk_index"].options["dims"] == 1024


def test_local_embedder_and_index_dims_agree(settings):
    assert settings.ports["embedder"].options["dims"] == settings.ports["chunk_index"].options["dims"] == 384


def test_every_local_endpoint_is_loopback(settings):
    assert set(settings.allowed_hosts) <= {"127.0.0.1", "localhost"}


def test_kb_env_selects_config(monkeypatch):
    monkeypatch.setenv("KB_ENV", "cml.example")
    assert load_settings().env == "cml"


def test_strict_mode_fails_on_unset_variable(monkeypatch, tmp_path: Path):
    raw = yaml.safe_load((CONFIG_DIR / "local.yaml").read_text())
    raw["services"]["postgres"]["admin_password"] = "${KB_TEST_DEFINITELY_UNSET}"
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="KB_TEST_DEFINITELY_UNSET"):
        load_settings("t", strict=True, config_dir=tmp_path)


def _variant(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load((CONFIG_DIR / "local.yaml").read_text())
    mutate(raw)
    (tmp_path / "v.yaml").write_text(yaml.safe_dump(raw))
    return tmp_path


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://my-resource.openai.azure.com/openai",
        "https://api.anthropic.com/v1",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://openrouter.ai/api/v1",
    ],
)
def test_hosted_llm_endpoint_is_rejected_even_if_allowlisted(tmp_path, url):
    from urllib.parse import urlparse

    def m(raw):
        raw["ports"]["llm"]["base_url"] = url
        raw["network"]["allowed_hosts"].append(urlparse(url).hostname)

    with pytest.raises(ConfigError, match="hosted AI endpoint"):
        load_settings("v", config_dir=_variant(tmp_path, m))


def test_non_allowlisted_host_is_rejected(tmp_path):
    def m(raw):
        raw["ports"]["chunk_index"]["url"] = "http://10.1.2.3:9200"

    with pytest.raises(ConfigError, match="not in network.allowed_hosts"):
        load_settings("v", config_dir=_variant(tmp_path, m))


def test_missing_port_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="ports missing"):
        load_settings("v", config_dir=_variant(tmp_path, lambda raw: raw["ports"].pop("tracer")))


def test_dev_header_auth_is_refused_outside_local(tmp_path):
    raw = yaml.safe_load((CONFIG_DIR / "cml.example.yaml").read_text())
    raw["gateway"] = {"auth": "dev_header"}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="local-only"):
        load_settings("c", config_dir=tmp_path)
