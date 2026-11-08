"""CLAUDE.md rule 2: no hosted LLM/embedding calls; subprocess environments are sanitized."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from kb import envsafe
from kb.config import HOSTED_AI_HOST_SUFFIXES, REPO_ROOT

SECRET_VARS = {
    "OPENAI_API_KEY": "sk-test",
    "ANTHROPIC_API_KEY": "x",
    "GEMINI_API_KEY": "x",
    "GOOGLE_API_KEY": "x",
    "AZURE_OPENAI_API_KEY": "x",
    "AWS_ACCESS_KEY_ID": "x",
    "AWS_SECRET_ACCESS_KEY": "x",
    "HF_TOKEN": "x",
    "MISTRAL_API_KEY": "x",
    "GROQ_API_KEY": "x",
    "OPENROUTER_API_KEY": "x",
    "KIMI_API_KEY": "x",
    "DEEPSEEK_API_KEY": "x",
    "COHERE_API_KEY": "x",
    "SOME_VENDOR_API_KEY": "x",
}


def test_sanitized_env_strips_every_credential_like_variable():
    env = envsafe.sanitized_env({**SECRET_VARS, "PATH": "/bin", "KB_ENV": "local"})
    assert set(env) == {"PATH", "KB_ENV"}


def test_sanitized_env_refuses_credential_in_extra():
    with pytest.raises(ValueError):
        envsafe.sanitized_env({}, extra={"OPENAI_API_KEY": "sk"})


def test_subprocess_sees_no_credentials(monkeypatch):
    for k, v in SECRET_VARS.items():
        monkeypatch.setenv(k, v)
    out = envsafe.run(
        [sys.executable, "-c", "import os,json;print(json.dumps(sorted(os.environ)))"], capture_output=True, text=True
    ).stdout
    leaked = set(json.loads(out)) & set(SECRET_VARS)
    assert not leaked


def test_env_kwarg_cannot_bypass_sanitizer():
    with pytest.raises(TypeError):
        envsafe.run([sys.executable, "-c", "pass"], env={"OPENAI_API_KEY": "x"})


def _source_files() -> list[Path]:
    roots = [REPO_ROOT / "src", REPO_ROOT / "synthetic", REPO_ROOT / "infra", REPO_ROOT / "config"]
    return [
        p
        for r in roots
        for p in r.rglob("*")
        if p.suffix in {".py", ".yaml", ".yml", ".ps1", ".sh"} and ".venv" not in p.parts  # our code only
    ]


def test_no_hosted_ai_hosts_in_code_or_config():
    allowed_mentions = {REPO_ROOT / "src" / "kb" / "config.py", REPO_ROOT / "src" / "kb" / "envsafe.py"}
    pat = re.compile("|".join(re.escape(s) for s in HOSTED_AI_HOST_SUFFIXES))
    hits = [
        f"{p}: {m.group(0)}"
        for p in _source_files()
        if p not in allowed_mentions
        for m in [pat.search(p.read_text(encoding="utf-8", errors="ignore"))]
        if m
    ]
    assert not hits, hits


def test_no_cloud_sdk_imports():
    pat = re.compile(
        r"^\s*(import|from)\s+(openai|anthropic|google\.generativeai|google\.genai|cohere|mistralai|"
        r"groq|boto3|langchain_openai)\b",
        re.M,
    )
    hits = [
        str(p)
        for p in _source_files()
        if p.suffix == ".py" and p not in OPENAI_CLIENT_LOOPBACK_ONLY and pat.search(p.read_text(encoding="utf-8"))
    ]
    assert not hits, hits


# D-057: RAGAS's judge adapter needs an openai client object. This ONE file may import it, pointed at the
# loopback Ollama endpoint, and only while it keeps its own refusals (asserted below); it runs in the isolated
# RAGAS env under the network guard (tests/security/test_ragas_isolation.py proves outbound calls fail).
OPENAI_CLIENT_LOOPBACK_ONLY = {REPO_ROOT / "infra" / "local" / "ragas" / "score.py"}


def test_the_openai_client_exception_keeps_its_compensating_checks():
    (p,) = OPENAI_CLIENT_LOOPBACK_ONLY
    src = p.read_text(encoding="utf-8")
    imports = re.findall(
        r"^\s*(?:import|from)\s+(openai|anthropic|google|cohere|mistralai|groq|boto3|langchain_openai)\b", src, re.M
    )
    assert imports == ["openai"], imports  # only the openai client, nothing else
    for check in (
        'RAGAS_DO_NOT_TRACK") != "true"',
        '"sitecustomize" not in sys.modules',
        "not in LOOPBACK",
        "os.path.isdir(args.emb_path)",
        'api_key="local-ollama"',
    ):
        assert check in src, check
