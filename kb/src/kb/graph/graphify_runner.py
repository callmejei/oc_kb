"""Run graphify safely (graphify-fibo skill, hard rule 1).

graphify auto-detects cloud API keys and defaults to hosted endpoints when no backend is given. Here it
always runs:
- from its own isolated environment (infra/local/graphify), as a subprocess;
- with a sanitized environment (kb.envsafe: no *_API_KEY / OPENAI_* / ANTHROPIC_* / ... variables);
- with an explicit `--backend ollama` and a loopback OLLAMA_BASE_URL taken from config;
- under the network guard (infra/local/graphify/guard/sitecustomize.py), which refuses and records every
  non-loopback connection; any recorded attempt fails the run.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kb import envsafe
from kb.config import REPO_ROOT

GRAPHIFY_ENV = REPO_ROOT / "infra" / "local" / "graphify"
GUARD_DIR = GRAPHIFY_ENV / "guard"


class GraphifyError(RuntimeError):
    pass


@dataclass
class GraphifyRun:
    graph_path: Path
    seconds: float
    blocked_attempts: list[dict[str, Any]] = field(default_factory=list)
    stdout_tail: str = ""

    def graph(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.graph_path.read_text(encoding="utf-8"))
        return data


def graphify_exe() -> Path:
    exe = (
        GRAPHIFY_ENV
        / ".venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("graphify.exe" if os.name == "nt" else "graphify")
    )
    if not exe.exists():
        raise GraphifyError(f"graphify env missing: run `uv sync --locked --project {GRAPHIFY_ENV}`")
    return exe


def guarded_env(
    base_url: str,
    model: str,
    netlog: Path,
    allowed_hosts: list[str],
    *,
    num_ctx: int = 12288,
    max_output_tokens: int = 3072,
    api_timeout_s: int = 300,
    seed: int = 42,
) -> dict[str, str]:
    """Env for the graphify subprocess. The Ollama knobs are pinned (not graphify's per-request sizing):
    graphify derives num_ctx = input + 16384 output + 2000 per chunk, which reloads the model on every
    request, spills layers off an 8 GB GPU, and lets a runaway generation run into Ollama's 10-min wall."""
    host = urlparse(base_url).hostname or ""
    if host not in allowed_hosts or host not in ("127.0.0.1", "localhost", "::1"):
        raise GraphifyError(f"graphify may only use a loopback LLM endpoint, not {base_url!r}")
    return {
        "OLLAMA_BASE_URL": base_url,
        "OLLAMA_MODEL": model,
        "PYTHONPATH": str(GUARD_DIR),
        "KB_NETGUARD_LOG": str(netlog),
        "PYTHONIOENCODING": "utf-8",
        "HF_HUB_OFFLINE": "1",
        "GRAPHIFY_OLLAMA_NUM_CTX": str(num_ctx),
        "GRAPHIFY_MAX_OUTPUT_TOKENS": str(max_output_tokens),
        "GRAPHIFY_API_TIMEOUT": str(api_timeout_s),
        # Qwen3 non-thinking sampling (greedy decoding loops on long JSON); a fixed seed keeps runs reproducible
        "GRAPHIFY_LLM_TEMPERATURE": "0.7",
        "GRAPHIFY_OLLAMA_SEED": str(seed),
        "GRAPHIFY_NO_TIPS": "1",
        "GRAPHIFY_NO_CTA": "1",
    }


def kill_tree(pid: int) -> None:
    """Kill a process and all its descendants (Windows: taskkill /T; POSIX: the process group)."""
    if sys.platform == "win32":
        envsafe.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)


def run_graphify(
    input_dir: Path,
    out_dir: Path,
    *,
    base_url: str,
    model: str,
    allowed_hosts: list[str],
    token_budget: int = 4000,
    timeout_s: int = 14400,
    num_ctx: int = 12288,
    max_output_tokens: int = 3072,
    api_timeout_s: int = 300,
    seed: int = 42,
) -> GraphifyRun:
    """Run graphify once over one BU's Markdown. `out_dir` is kept between runs on purpose: graphify's
    per-file semantic cache lives there, so an interrupted or repeated build resumes instead of
    re-extracting. Progress is streamed to out_dir/graphify.log."""
    out_dir.mkdir(parents=True, exist_ok=True)
    netlog = out_dir / "netguard.log"
    netlog.unlink(missing_ok=True)
    env = guarded_env(
        base_url,
        model,
        netlog,
        allowed_hosts,
        num_ctx=num_ctx,
        max_output_tokens=max_output_tokens,
        api_timeout_s=api_timeout_s,
        seed=seed,
    )
    log_path = out_dir / "graphify.log"
    t0 = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        proc = envsafe.popen(
            [
                str(graphify_exe()),
                "extract",
                str(input_dir),
                "--backend",
                "ollama",
                "--model",
                model,
                "--max-concurrency",
                "1",
                "--token-budget",
                str(token_budget),
                "--no-cluster",
                "--out",
                str(out_dir),
            ],
            extra_env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",  # own process group, so kill_tree reaches the workers
        )
        try:
            rc = proc.wait(timeout=timeout_s)
        except BaseException:
            kill_tree(proc.pid)  # graphify.exe is a launcher; killing only it orphans the python worker
            proc.wait()
            raise
    secs = time.monotonic() - t0
    blocked = [json.loads(ln) for ln in netlog.read_text(encoding="utf-8").splitlines()] if netlog.exists() else []
    if blocked:
        raise GraphifyError(f"graphify attempted {len(blocked)} outbound connection(s): {blocked[:3]}")
    graph = out_dir / "graphify-out" / "graph.json"
    out = log_path.read_text(encoding="utf-8", errors="replace")
    if rc != 0 or not graph.exists():
        raise GraphifyError(f"graphify failed (rc={rc}): {out[-800:]}")
    return GraphifyRun(graph, secs, blocked, out[-1500:])
