"""Sanitized subprocess environments (CLAUDE.md rule 2).

Tools such as graphify auto-detect cloud API keys from the environment and silently use a
hosted model. Every subprocess the platform starts goes through ``sanitized_env``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

_BLOCKED = re.compile(
    r"""^(
        .*_API_KEY | .*_API_TOKEN | .*_ACCESS_TOKEN | .*_SECRET_KEY |
        OPENAI_.* | AZURE_OPENAI_.* | ANTHROPIC_.* | CLAUDE_.* | GEMINI_.* | GOOGLE_API_.* |
        GOOGLE_APPLICATION_CREDENTIALS | VERTEX_.* | COHERE_.* | MISTRAL_.* | GROQ_.* |
        TOGETHER_.* | DEEPSEEK_.* | MOONSHOT_.* | KIMI_.* | OPENROUTER_.* | HF_TOKEN |
        HUGGING_FACE_HUB_TOKEN | HUGGINGFACE_.*TOKEN | AWS_.* | BEDROCK_.* | LANGCHAIN_.* |
        LANGSMITH_.*
    )$""",
    re.VERBOSE | re.IGNORECASE,
)


def is_blocked(name: str) -> bool:
    return bool(_BLOCKED.match(name))


def sanitized_env(base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy of ``base`` (default os.environ) without credential-like variables.

    ``extra`` is applied afterwards but is also checked: passing a blocked key raises.
    HF downloads are forced offline-capable but not blocked; model weights are pinned separately.
    """
    src = dict(os.environ if base is None else base)
    env = {k: v for k, v in src.items() if not is_blocked(k)}
    for k, v in (extra or {}).items():
        if is_blocked(k):
            raise ValueError(f"refusing to pass credential-like variable {k} to a subprocess")
        env[k] = v
    return env


def run(
    cmd: Sequence[str], *, extra_env: Mapping[str, str] | None = None, **kw: Any
) -> subprocess.CompletedProcess[Any]:
    """subprocess.run with a sanitized environment. ``env=`` is not accepted."""
    if "env" in kw:
        raise TypeError("pass extra_env, not env")
    return subprocess.run(list(cmd), env=sanitized_env(extra=extra_env), check=kw.pop("check", True), **kw)  # noqa: S603


def popen(cmd: Sequence[str], *, extra_env: Mapping[str, str] | None = None, **kw: Any) -> subprocess.Popen[Any]:
    if "env" in kw:
        raise TypeError("pass extra_env, not env")
    return subprocess.Popen(list(cmd), env=sanitized_env(extra=extra_env), **kw)  # noqa: S603
