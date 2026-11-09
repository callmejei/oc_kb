"""graphify sandbox (skill hard rule 1): sanitized env, loopback-only endpoint, network guard."""

from __future__ import annotations

import json

import pytest

from kb import envsafe
from kb.graph.graphify_runner import GRAPHIFY_ENV, GraphifyError, graphify_exe, guarded_env

PY = GRAPHIFY_ENV / ".venv" / "Scripts" / "python.exe"


def test_env_has_no_credentials_and_a_loopback_endpoint(tmp_path, monkeypatch):
    for k in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "MOONSHOT_API_KEY",
        "DEEPSEEK_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "OLLAMA_API_KEY",
    ):
        monkeypatch.setenv(k, "leak-me")
    extra = guarded_env("http://127.0.0.1:11435/v1", "qwen3:8b", tmp_path / "n.log", ["127.0.0.1", "localhost"])
    env = envsafe.sanitized_env(extra=extra)
    assert not [k for k in env if envsafe.is_blocked(k)]
    assert env["OLLAMA_BASE_URL"] == "http://127.0.0.1:11435/v1" and env["PYTHONPATH"].endswith("guard")


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://10.0.0.5:11434/v1", "http://ollama.example/v1"])
def test_non_loopback_endpoints_are_refused(tmp_path, url):
    with pytest.raises(GraphifyError, match="loopback"):
        guarded_env(url, "m", tmp_path / "n.log", ["127.0.0.1", "localhost", "ollama.example", "10.0.0.5"])


def test_network_guard_blocks_and_records_outbound_in_the_graphify_env(tmp_path):
    graphify_exe()  # the isolated env exists
    log = tmp_path / "net.log"
    extra = guarded_env("http://127.0.0.1:11435/v1", "m", log, ["127.0.0.1"])
    code = (
        "import os, socket, urllib.request\n"
        "assert os.environ.get('KB_NETGUARD_ACTIVE') == '1'\n"
        "for target in ('https://example.com', 'https://api.openai.com/v1/models'):\n"
        "    try: urllib.request.urlopen(target, timeout=3); print('LEAK')\n"
        "    except Exception: pass\n"
        "print(urllib.request.urlopen('http://127.0.0.1:11435/api/version', timeout=5).status)\n"
    )
    r = envsafe.run([str(PY), "-c", code], extra_env=extra, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "LEAK" not in r.stdout
    assert r.stdout.strip().endswith("200")
    recorded = [json.loads(x)["target"] for x in log.read_text().splitlines()]
    assert set(recorded) == {"example.com", "api.openai.com"}


def test_kill_tree_reaps_grandchildren():
    """graphify.exe is a launcher: a timeout must kill its python worker too, not orphan it."""
    import subprocess
    import sys
    import time

    import psutil

    from kb.graph.graphify_runner import kill_tree

    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(c.pid, flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=sys.platform != "win32",
    )
    child = int(parent.stdout.readline())
    assert psutil.pid_exists(child)
    kill_tree(parent.pid)
    parent.wait(timeout=10)
    deadline = time.time() + 10
    while psutil.pid_exists(child) and psutil.Process(child).status() != psutil.STATUS_ZOMBIE:
        assert time.time() < deadline, "grandchild survived"
        time.sleep(0.2)
