from __future__ import annotations

import pytest

from kb.adapters import adapter_class, build, known_adapters
from kb.adapters._stub import NotImplementedYetError
from kb.config import PORT_NAMES, load_settings
from kb.ports import PORT_PROTOCOLS


def test_every_port_has_a_protocol():
    assert set(PORT_PROTOCOLS) == set(PORT_NAMES)


@pytest.mark.parametrize("env", ["local", "cml.example"])
def test_every_configured_adapter_is_known(env):
    s = load_settings(env)
    for port, pc in s.ports.items():
        assert pc.adapter in known_adapters(), f"{env}:{port} -> {pc.adapter}"


@pytest.mark.parametrize("port", PORT_NAMES)
def test_local_adapter_builds_and_satisfies_protocol(settings, port):
    obj = build(settings, port)
    assert isinstance(obj, PORT_PROTOCOLS[port])
    assert obj.STATUS in ("implemented", "stubbed")


def test_stubs_fail_loudly_with_their_phase():
    stub = adapter_class("cml.StarburstSqlEngine")(endpoint="x")  # PhoenixTracer was the example until Phase 7
    with pytest.raises(NotImplementedYetError, match="Phase 7"):
        stub.execute("SELECT 1", None, row_limit=1)
    assert adapter_class("local.OxigraphGraphStore").STATUS == "implemented"


def test_unknown_adapter_raises():
    with pytest.raises(KeyError):
        adapter_class("local.DoesNotExist")
