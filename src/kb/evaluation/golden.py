"""Golden-set evaluation: retrieval hit@k, answer correctness, citation correctness, latency.

Usage: python -m kb.evaluation.golden --out docs/eval/baseline-phase3.json
Runs against the live local index with the real local models (no audit rows are written).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.agent.answer import answer
from kb.config import REPO_ROOT, Settings, load_settings
from kb.gateway.acl import build_access_filter
from kb.retrieval.pipeline import HybridRetriever
from synthetic.world import DOC_BY_KEY

GOLDEN = REPO_ROOT / "tests" / "eval" / "golden" / "golden.yaml"


def load_golden(path: Path = GOLDEN) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def doc_title(key: str) -> str:
    return DOC_BY_KEY[key].filename.rsplit(".", 1)[0]


def evaluate(settings: Settings, k: int = 5, log: Any = print) -> dict[str, Any]:
    idx = build(settings, "chunk_index")
    hr = HybridRetriever(idx, build(settings, "embedder"), build(settings, "reranker"), settings.retrieval)
    llm = build(settings, "llm")
    idp = YamlIdentityProvider("config/users.yaml")
    rows = []
    for bu, block in load_golden().items():
        flt = build_access_filter(idp.resolve(block["user"]))
        for q in block["questions"]:
            titles = {doc_title(d) for d in q["expected_docs"]}
            t0 = time.monotonic()
            r = hr.retrieve(q["q"], flt)
            a = answer(q["q"], r.context, llm, int(settings.retrieval.get("max_context_chars", 9000)))
            ms = int((time.monotonic() - t0) * 1000)
            ctx_titles = [c.chunk.metadata.get("title") for c in r.context[:k]]
            row = {
                "bu": bu,
                "id": q["id"],
                "question": q["q"],
                "hit_at_k": any(t in titles for t in ctx_titles),
                "correct": any(f.lower() in a.text.lower() for f in q["facts"]),
                "citation_correct": any(c.title in titles for c in a.citations),
                "grounded": a.grounded,
                "blocked_reason": a.blocked_reason,
                "latency_ms": ms,
                "answer": a.text,
                "cited": [f"{c.title} | {c.section} | p{c.page} | v{c.version_no}" for c in a.citations],
            }
            rows.append(row)
            log(
                f"{q['id']:<4} hit={row['hit_at_k']!s:<5} correct={row['correct']!s:<5} "
                f"cite={row['citation_correct']!s:<5} {ms:>6} ms  {a.text[:90]!r}"
            )

    def agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rs)
        return {
            "n": n,
            f"hit@{k}": round(sum(r["hit_at_k"] for r in rs) / n, 3),
            "answer_correct": round(sum(r["correct"] for r in rs) / n, 3),
            "citation_correct": round(sum(r["citation_correct"] for r in rs) / n, 3),
            "median_latency_ms": int(statistics.median(r["latency_ms"] for r in rs)),
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": {
            "llm": settings.ports["llm"].options.get("model"),
            "embedder": f"{settings.ports['embedder'].options.get('model')}@"
            f"{str(settings.ports['embedder'].options.get('revision'))[:10]}",
            "reranker": f"{settings.ports['reranker'].options.get('model')}@"
            f"{str(settings.ports['reranker'].options.get('revision'))[:10]}",
            "retrieval": settings.retrieval,
        },
        "overall": agg(rows),
        "by_bu": {bu: agg([r for r in rows if r["bu"] == bu]) for bu in sorted({r["bu"] for r in rows})},
        "questions": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.evaluation.golden")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    report = evaluate(load_settings(strict=True))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "by_bu": report["by_bu"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
