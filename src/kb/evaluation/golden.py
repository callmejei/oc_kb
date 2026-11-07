"""Golden-set evaluation: retrieval hit@k, answer correctness, citation correctness, latency.

Usage: python -m kb.evaluation.golden --out docs/eval/baseline-phase3.json
Runs against the live local index with the real local models (no audit rows are written).
"""

from __future__ import annotations

import argparse
import json
import re
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


EXPANSION = REPO_ROOT / "tests" / "eval" / "golden" / "expansion.yaml"


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
    ap.add_argument("--suite", choices=["documents", "structured", "expansion"], default="documents")
    a = ap.parse_args(argv)
    settings = load_settings(strict=True)
    if a.suite in ("structured", "expansion"):
        report = evaluate_structured(settings) if a.suite == "structured" else evaluate_expansion(settings)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(report, indent=1, default=str) + chr(10), encoding="utf-8")
        print(json.dumps(report["overall"], indent=1))
        return 0
    report = evaluate(settings)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "by_bu": report["by_bu"]}, indent=1))
    return 0


# ================================================================================================= structured
STRUCTURED = REPO_ROOT / "tests" / "eval" / "golden" / "structured.yaml"
_NUMS = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _nums(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in _NUMS.findall(text)]


def figure_found(expected: float, text: str, rel: float = 0.005) -> bool:
    return any(abs(v - expected) <= max(0.01, abs(expected) * rel) for v in _nums(text))


def evaluate_structured(settings: Settings, log: Any = print) -> dict[str, Any]:
    import duckdb

    from kb.agent.service import AskService
    from kb.sql.text_to_sql import TextToSql

    idx = build(settings, "chunk_index")
    llm = build(settings, "llm")
    svc = AskService(
        HybridRetriever(idx, build(settings, "embedder"), build(settings, "reranker"), settings.retrieval),
        llm,
        build(settings, "registry"),  # lazy: compute() never writes the audit
        settings.retrieval,
        TextToSql(build(settings, "sql_engine"), llm),
    )
    idp = YamlIdentityProvider("config/users.yaml")
    con = duckdb.connect(str(settings.path("data_dir") / "marts" / "marts.duckdb"), read_only=True)
    rows = []
    for item in yaml.safe_load(STRUCTURED.read_text(encoding="utf-8")):
        expected = float(con.execute(item["truth_sql"]).fetchone()[0])  # type: ignore[index]
        t0 = time.monotonic()
        c = svc.compute(idp.resolve(item["user"]), item["q"])
        ms = int((time.monotonic() - t0) * 1000)
        kinds = {x.kind for x in c.answer.citations}
        row = {
            "id": item["id"],
            "question": item["q"],
            "expected": expected,
            "route_expected": item["route"],
            "route": c.route,
            "route_correct": c.route == item["route"],
            "data_correct": figure_found(expected, c.answer.text),
            "doc_correct": item.get("doc_fact", "") in c.answer.text if item.get("doc_fact") else None,
            "citations_ok": kinds == ({"document", "data"} if item["route"] == "both" else {"data"}),
            "blocked_reason": c.answer.blocked_reason,
            "latency_ms": ms,
            "answer": c.answer.text,
            "sql": [a.get("sql") for a in c.sql.attempts] if c.sql else [],
        }
        rows.append(row)
        log(
            f"{item['id']:<4} route={c.route:<9} ok_route={row['route_correct']!s:<5} data={row['data_correct']!s:<5} "
            f"doc={row['doc_correct']!s:<5} cites={row['citations_ok']!s:<5} {ms:>6} ms {c.answer.text[:80]!r}"
        )
    con.close()
    n = len(rows)
    both = [r for r in rows if r["route_expected"] == "both"]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "overall": {
            "n": n,
            "route_correct": round(sum(r["route_correct"] for r in rows) / n, 3),
            "data_correct": round(sum(r["data_correct"] for r in rows) / n, 3),
            "doc_correct_on_combined": round(sum(bool(r["doc_correct"]) for r in both) / max(1, len(both)), 3),
            "citations_ok": round(sum(r["citations_ok"] for r in rows) / n, 3),
            "median_latency_ms": int(statistics.median(r["latency_ms"] for r in rows)),
        },
        "questions": rows,
    }


# ================================================================================================= expansion
def evaluate_expansion(settings: Settings, k: int = 5, log: Any = print) -> dict[str, Any]:
    """Each paraphrase question is answered twice: without and with graph expansion."""
    from kb.gateway.acl import graph_scope
    from kb.graph.ontology import Ontology
    from kb.retrieval.expansion import GraphExpander

    idx = build(settings, "chunk_index")
    emb, rr, llm = build(settings, "embedder"), build(settings, "reranker"), build(settings, "llm")
    expander = GraphExpander(build(settings, "graph_store"), Ontology(), int(settings.graph.get("expansion_terms", 10)))
    plain = HybridRetriever(idx, emb, rr, settings.retrieval)
    expanded = HybridRetriever(idx, emb, rr, settings.retrieval, expander)
    idp = YamlIdentityProvider("config/users.yaml")
    rows = []
    for bu, block in load_golden(EXPANSION).items():
        ident = idp.resolve(block["user"])
        flt = build_access_filter(ident)
        scope = graph_scope(ident, idp.readable_bus(ident))
        for q in block["questions"]:
            titles = {doc_title(d) for d in q["expected_docs"]}
            row: dict[str, Any] = {"bu": bu, "id": q["id"], "question": q["q"]}
            for mode, r in (("off", plain.retrieve(q["q"], flt)), ("on", expanded.retrieve(q["q"], flt, scope=scope))):
                a = answer(q["q"], r.context, llm, int(settings.retrieval.get("max_context_chars", 9000)))
                row[mode] = {
                    "hit_at_k": any(c.chunk.metadata.get("title") in titles for c in r.context[:k]),
                    "correct": any(f.lower() in a.text.lower() for f in q["facts"]),
                    "answer": a.text,
                    "expansion": None
                    if r.expansion is None
                    else {"concepts": r.expansion.concepts, "related": r.expansion.related, "terms": r.expansion.terms},
                }
            row["improved"] = (row["on"]["correct"] and not row["off"]["correct"]) or (
                row["on"]["hit_at_k"] and not row["off"]["hit_at_k"]
            )
            row["regressed"] = (row["off"]["correct"] and not row["on"]["correct"]) or (
                row["off"]["hit_at_k"] and not row["on"]["hit_at_k"]
            )
            rows.append(row)
            log(
                f"{q['id']:<4} off: hit={row['off']['hit_at_k']!s:<5} ok={row['off']['correct']!s:<5} | "
                f"on: hit={row['on']['hit_at_k']!s:<5} ok={row['on']['correct']!s:<5} improved={row['improved']}"
            )
    n = len(rows)

    def rate(mode: str, key: str) -> float:
        return float(round(sum(r[mode][key] for r in rows) / n, 3))

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "overall": {
            "n": n,
            "off": {f"hit@{k}": rate("off", "hit_at_k"), "answer_correct": rate("off", "correct")},
            "on": {f"hit@{k}": rate("on", "hit_at_k"), "answer_correct": rate("on", "correct")},
            "improved": [r["id"] for r in rows if r["improved"]],
            "regressed": [r["id"] for r in rows if r["regressed"]],
        },
        "questions": rows,
    }


if __name__ == "__main__":
    sys.exit(main())
