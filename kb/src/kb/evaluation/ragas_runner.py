"""RAGAS metrics, scored by a LOCAL judge in an isolated environment (D-057).

RAGAS 0.4.3 brings langchain, langsmith and the openai client, and sends usage telemetry by default. It
therefore never runs in the platform's own environment: ``infra/local/ragas/score.py`` runs in
``infra/local/ragas/.venv`` as a subprocess with
- a sanitized environment (kb.envsafe: no credential-like variables), RAGAS_DO_NOT_TRACK=true, LangSmith
  tracing impossible (envsafe strips every LANGCHAIN_/LANGSMITH_ variable), HF offline;
- the network guard on PYTHONPATH, which refuses and records any non-loopback connection or DNS lookup;
- a loopback judge (Ollama, ``evaluation.judge_model``) and the embedder loaded from its local path.
Any recorded outbound attempt fails the run (fail closed), even though the attempt itself was blocked.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kb import envsafe
from kb.config import REPO_ROOT, Settings
from kb.graph.graphify_runner import GUARD_DIR

RAGAS_ENV = REPO_ROOT / "infra" / "local" / "ragas"
SCORER = RAGAS_ENV / "score.py"
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class RagasError(RuntimeError):
    pass


@dataclass
class RagasRun:
    scores: dict[str, dict[str, float | None]]
    errors: dict[str, dict[str, str]]
    blocked_attempts: list[dict[str, Any]] = field(default_factory=list)
    stdout_tail: str = ""


def ragas_python() -> Path:
    py = RAGAS_ENV / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        raise RagasError(f"RAGAS env missing: run `uv sync --locked --project {RAGAS_ENV}`")
    return py


def guarded_env(netlog: Path) -> dict[str, str]:
    return {
        "PYTHONPATH": str(GUARD_DIR),
        "KB_NETGUARD_LOG": str(netlog),
        "RAGAS_DO_NOT_TRACK": "true",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def score(samples: list[dict[str, Any]], settings: Settings, workdir: Path) -> RagasRun:
    """samples: {id, user_input, response, retrieved_contexts, reference, metrics?}"""
    ev = settings.evaluation
    judge_url = str(ev["judge_base_url"])
    if (urlparse(judge_url).hostname or "") not in _LOOPBACK:
        raise RagasError(f"the RAGAS judge must be a loopback endpoint, not {judge_url!r}")
    emb_path = REPO_ROOT / str(settings.ports["embedder"].options["path"])
    workdir.mkdir(parents=True, exist_ok=True)
    inp, out, netlog = workdir / "ragas_in.jsonl", workdir / "ragas_out.jsonl", workdir / "netguard.log"
    for p in (out, netlog):
        p.unlink(missing_ok=True)
    inp.write_text("".join(json.dumps(s) + "\n" for s in samples), encoding="utf-8")
    cmd = [
        str(ragas_python()),
        str(SCORER),
        "--inp",
        str(inp),
        "--out",
        str(out),
        "--judge-url",
        judge_url,
        "--judge-model",
        str(ev["judge_model"]),
        "--emb-path",
        str(emb_path),
    ]
    proc = envsafe.run(
        cmd,
        extra_env=guarded_env(netlog),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(ev.get("ragas_timeout_s", 10800)),
        check=False,
    )
    blocked = [json.loads(line) for line in netlog.read_text(encoding="utf-8").splitlines()] if netlog.exists() else []
    tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
    if blocked:
        raise RagasError(f"RAGAS attempted {len(blocked)} outbound connection(s), blocked: {blocked[:3]}")
    if proc.returncode != 0:
        raise RagasError(f"RAGAS scorer exited {proc.returncode}: {tail[-1500:]}")
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    return RagasRun(
        scores={r["id"]: r["scores"] for r in rows},
        errors={r["id"]: r["errors"] for r in rows if r["errors"]},
        blocked_attempts=blocked,
        stdout_tail=tail,
    )
