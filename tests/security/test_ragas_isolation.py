"""D-057: the RAGAS environment cannot reach the network, and the runner refuses to start without the
guard, the telemetry opt-out or a loopback judge."""

from __future__ import annotations

import json

import pytest

from kb import envsafe
from kb.config import load_settings
from kb.evaluation.ragas_runner import SCORER, RagasError, guarded_env, ragas_python, score

PROBE = (
    "import urllib.request\n"
    "try:\n"
    "    urllib.request.urlopen('https://t.explodinggradients.com', timeout=5)\n"
    "    print('REACHED')\n"
    "except Exception as e:\n"
    "    print('BLOCKED', type(e).__name__)\n"
)


def test_outbound_call_from_the_ragas_env_is_blocked_and_recorded(tmp_path):
    netlog = tmp_path / "netguard.log"
    p = envsafe.run(
        [str(ragas_python()), "-c", PROBE], extra_env=guarded_env(netlog), capture_output=True, text=True, check=False
    )
    assert "BLOCKED" in p.stdout and "REACHED" not in p.stdout, p.stdout + p.stderr
    attempts = [json.loads(x) for x in netlog.read_text(encoding="utf-8").splitlines()]
    assert attempts and any("explodinggradients" in a["target"] for a in attempts)


def test_scorer_refuses_without_guard_or_telemetry_opt_out(tmp_path):
    common = ["--inp", "x", "--out", "y", "--judge-url", "http://127.0.0.1:11435/v1", "--judge-model", "m",
              "--emb-path", str(tmp_path)]  # fmt: skip
    no_guard = envsafe.run([str(ragas_python()), str(SCORER), *common], extra_env={"RAGAS_DO_NOT_TRACK": "true"},
                           capture_output=True, text=True, check=False)  # fmt: skip
    assert no_guard.returncode == 2 and "network guard" in no_guard.stderr
    env = guarded_env(tmp_path / "n.log")
    env.pop("RAGAS_DO_NOT_TRACK")
    no_opt_out = envsafe.run([str(ragas_python()), str(SCORER), *common], extra_env=env, capture_output=True,
                             text=True, check=False)  # fmt: skip
    assert no_opt_out.returncode == 2 and "RAGAS_DO_NOT_TRACK" in no_opt_out.stderr


def test_scorer_refuses_a_non_loopback_judge(tmp_path):
    p = envsafe.run(
        [str(ragas_python()), str(SCORER), "--inp", "x", "--out", "y", "--judge-url", "https://judge.example.com/v1",
         "--judge-model", "m", "--emb-path", str(tmp_path)],
        extra_env=guarded_env(tmp_path / "n.log"), capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert p.returncode == 2 and "not loopback" in p.stderr


def test_runner_refuses_a_non_loopback_judge_before_starting(tmp_path):
    s = load_settings()
    bad = s.model_copy(update={"evaluation": s.evaluation | {"judge_base_url": "https://api.example.com/v1"}})
    with pytest.raises(RagasError, match="loopback"):
        score([], bad, tmp_path)
