"""RAGAS scorer. Runs ONLY inside infra/local/ragas/.venv, started by kb.evaluation.ragas_runner with a
sanitized environment and the network guard (infra/local/graphify/guard/sitecustomize.py) on PYTHONPATH.

Input: JSONL samples {id, user_input, response, retrieved_contexts, reference, metrics}.
Output: JSONL {id, scores: {metric: float|null}, errors: {metric: str}}, one line per sample, flushed as it goes.
The judge is a local OpenAI-compatible endpoint (Ollama); the embedder loads from a local path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def _refuse(msg: str) -> None:
    print(f"ragas scorer refused: {msg}", file=sys.stderr)
    sys.exit(2)


async def _score(args: argparse.Namespace) -> int:
    from openai import AsyncOpenAI
    from ragas.embeddings import HuggingFaceEmbeddings
    from ragas.llms import llm_factory
    from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

    client = AsyncOpenAI(base_url=args.judge_url, api_key="local-ollama", timeout=args.request_timeout, max_retries=1)
    llm = llm_factory(args.judge_model, provider="openai", client=client, temperature=0.0, max_tokens=2048)
    emb = HuggingFaceEmbeddings(model=args.emb_path, device="cpu")
    m = {
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=emb),
        "context_precision": ContextPrecision(llm=llm),
        "context_recall": ContextRecall(llm=llm),
    }
    calls = {
        "faithfulness": lambda s: m["faithfulness"].ascore(
            user_input=s["user_input"], response=s["response"], retrieved_contexts=s["retrieved_contexts"]
        ),
        "answer_relevancy": lambda s: m["answer_relevancy"].ascore(user_input=s["user_input"], response=s["response"]),
        "context_precision": lambda s: m["context_precision"].ascore(
            user_input=s["user_input"], reference=s["reference"], retrieved_contexts=s["retrieved_contexts"]
        ),
        "context_recall": lambda s: m["context_recall"].ascore(
            user_input=s["user_input"], retrieved_contexts=s["retrieved_contexts"], reference=s["reference"]
        ),
    }
    with open(args.inp, encoding="utf-8") as f:
        samples = [json.loads(line) for line in f if line.strip()]
    with open(args.out, "w", encoding="utf-8") as out:
        for s in samples:
            scores: dict[str, float | None] = {}
            errors: dict[str, str] = {}
            t0 = time.monotonic()
            for name in s.get("metrics", METRICS):
                try:
                    r = await calls[name](s)
                    v = float(r.value)
                    scores[name] = None if math.isnan(v) else round(v, 4)
                except Exception as e:  # noqa: BLE001 - one failed metric must not lose the run
                    scores[name] = None
                    errors[name] = f"{type(e).__name__}: {str(e)[:300]}"
            out.write(
                json.dumps(
                    {"id": s["id"], "scores": scores, "errors": errors, "seconds": round(time.monotonic() - t0, 1)}
                )
                + "\n"
            )
            out.flush()
            print(f"{s['id']}: {scores}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--judge-url", required=True)
    ap.add_argument("--judge-model", required=True)
    ap.add_argument("--emb-path", required=True)
    ap.add_argument("--request-timeout", type=float, default=300.0)
    args = ap.parse_args()
    if os.environ.get("RAGAS_DO_NOT_TRACK") != "true":
        _refuse("RAGAS_DO_NOT_TRACK must be 'true' (RAGAS sends usage telemetry otherwise)")
    if "sitecustomize" not in sys.modules or not os.environ.get("KB_NETGUARD_LOG"):
        _refuse("the network guard is not loaded")
    if (urlparse(args.judge_url).hostname or "") not in LOOPBACK:
        _refuse(f"judge endpoint {args.judge_url!r} is not loopback")
    if not os.path.isdir(args.emb_path):
        _refuse(f"embedder path {args.emb_path!r} is not a local directory")
    return asyncio.run(_score(args))


if __name__ == "__main__":
    sys.exit(main())
