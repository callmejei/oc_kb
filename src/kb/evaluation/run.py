"""`make eval` / `kb.ps1 eval`: the Phase 7 evaluation run.

1. Golden sets per BU (tests/eval/golden/<bu>.yaml), each question asked as that BU's user through the same
   AskService.compute path the gateway uses (router, text-to-SQL, ACL pre-filter, graph expansion as
   configured). No audit rows are written.
2. RAGAS (faithfulness, answer relevancy, context precision, context recall) on the answered document
   questions, scored by the local judge in the isolated, network-guarded env (kb.evaluation.ragas_runner).
3. The ACL leak suite: the retrieval matrix and the real-LLM matrix, run with pytest; every failure counts.
4. Thresholds from config `evaluation.thresholds` decide PASS / FAIL (exit code 1 on FAIL).
5. Report: docs/eval/report-<ts>.json and .html; the run is recorded in kb.eval_run / kb.eval_result
   (migration 009) as the pipeline role.
"""

from __future__ import annotations

import argparse
import html
import json
import statistics
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET  # noqa: S405 - parses the junit file pytest just wrote
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from kb import envsafe
from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.adapters.local.registry import PostgresRegistry
from kb.agent.answer import NO_CONTEXT, NOT_COVERED, REFUSED
from kb.config import REPO_ROOT, Settings, load_settings
from kb.evaluation.golden import BUS, doc_title, fact_correct, figure_found, load_bu
from kb.evaluation.ragas_runner import METRICS as RAGAS_METRICS
from kb.evaluation.ragas_runner import score as ragas_score

ABSTAINED = {NO_CONTEXT, NOT_COVERED, REFUSED}
K = 5


# ------------------------------------------------------------------------------------------------ asking
def build_service(settings: Settings) -> tuple[Any, YamlIdentityProvider]:
    from kb.agent.service import AskService
    from kb.gateway.acl import graph_scope
    from kb.graph.ontology import Ontology
    from kb.graph.table_map import TableMap
    from kb.retrieval.expansion import GraphExpander
    from kb.retrieval.pipeline import HybridRetriever
    from kb.sql.text_to_sql import TextToSql

    idp = YamlIdentityProvider("config/users.yaml")
    llm = build(settings, "llm")
    ontology = Ontology()
    expander = None
    if settings.graph.get("expansion"):
        expander = GraphExpander(
            build(settings, "graph_store"), ontology, int(settings.graph.get("expansion_terms", 10))
        )
    retriever = HybridRetriever(
        build(settings, "chunk_index"),
        build(settings, "embedder"),
        build(settings, "reranker"),
        settings.retrieval,
        expander,
    )
    t2s = TextToSql(build(settings, "sql_engine"), llm, table_map=TableMap(ontology))
    scope = (lambda ident: graph_scope(ident, idp.readable_bus(ident))) if expander else None
    svc = AskService(retriever, llm, build(settings, "registry"), settings.retrieval, t2s, scope_fn=scope)
    return svc, idp


def ask_one(svc: Any, ident: Any, q: dict[str, Any], truth: Any) -> dict[str, Any]:
    t0 = time.monotonic()
    c = svc.compute(ident, q["q"])
    ms = int((time.monotonic() - t0) * 1000)
    a = c.answer
    row: dict[str, Any] = {
        "id": q["id"],
        "kind": q["kind"],
        "question": q["q"],
        "route": c.route,
        "answer": a.text,
        "grounded": a.grounded,
        "blocked_reason": a.blocked_reason,
        "latency_ms": ms,
        "cited": [
            f"{x.title} | p{x.page} | v{x.version_no}" if x.kind == "document" else f"{x.n} {x.tables}"
            for x in a.citations
        ],  # fmt: skip
    }
    if q["kind"] in ("documents", "expansion"):
        titles = {doc_title(d) for d in q["expected_docs"]}
        row |= {
            "hit_at_k": any(s.chunk.metadata.get("title") in titles for s in c.context[:K]),
            "correct": fact_correct(q, a),
            "citation_correct": any(x.title in titles for x in a.citations),
            "reference": q["reference"],
            "contexts": [s.chunk.text for s in c.context if s.chunk.chunk_id in set(a.context_ids)],
        }
    elif q["kind"] == "abstain":
        row["correct"] = a.text in ABSTAINED
    else:  # data / both
        expected = float(truth(q["truth_sql"]))
        kinds = {x.kind for x in a.citations}
        row |= {
            "expected": expected,
            "data_correct": figure_found(expected, a.text),
            "route_correct": c.route == q["kind"],
            "doc_correct": (q["doc_fact"] in a.text) if q.get("doc_fact") else None,
            "citations_ok": kinds == ({"document", "data"} if q["kind"] == "both" else {"data"}),
        }
    return row


# ------------------------------------------------------------------------------------------------ leak suite
def run_leak_suite(workdir: Path) -> dict[str, Any]:
    junit = workdir / "leak-junit.xml"
    cmd = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "leak_llm or not leak_llm",
        "tests/integration/qa/test_leak_suite.py", "tests/integration/qa/test_leak_llm.py", f"--junitxml={junit}",
    ]  # fmt: skip
    t0 = time.monotonic()
    p = envsafe.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, cwd=REPO_ROOT)
    root = ET.parse(junit).getroot()  # noqa: S314
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None
    tests, failures, errors = (int(suite.get(k, "0")) for k in ("tests", "failures", "errors"))
    failed = [
        f"{tc.get('classname')}::{tc.get('name')}"
        for tc in suite.iter("testcase")
        if tc.find("failure") is not None or tc.find("error") is not None
    ]
    return {
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "leaks": failures + errors,
        "failed": failed,
        "exit_code": p.returncode,
        "seconds": int(time.monotonic() - t0),
        "tail": (p.stdout or "")[-1500:],
    }


# ------------------------------------------------------------------------------------------------ scoring
def _rate(rows: list[dict[str, Any]], key: str) -> tuple[float | None, int]:
    vals = [bool(r[key]) for r in rows if r.get(key) is not None]
    return (round(sum(vals) / len(vals), 3), len(vals)) if vals else (None, 0)


def _mean(vals: list[float | None]) -> tuple[float | None, int]:
    xs = [v for v in vals if v is not None]
    return (round(statistics.fmean(xs), 3), len(xs)) if xs else (None, 0)


def metrics_for(rows: list[dict[str, Any]], bu: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(suite: str, metric: str, vn: tuple[float | None, int]) -> None:
        if vn[0] is not None:
            out.append({"bu": bu, "suite": suite, "metric": metric, "value": vn[0], "n": vn[1]})

    docs = [r for r in rows if r["kind"] == "documents"]
    exp = [r for r in rows if r["kind"] == "expansion"]
    abst = [r for r in rows if r["kind"] == "abstain"]
    data = [r for r in rows if r["kind"] in ("data", "both")]
    for key, name in (
        ("hit_at_k", f"hit@{K}"),
        ("correct", "answer_correct"),
        ("citation_correct", "citation_correct"),
    ):
        add("golden", name, _rate(docs, key))
        add("expansion", name, _rate(exp, key))
    add("golden", "abstain_correct", _rate(abst, "correct"))
    for key in ("data_correct", "route_correct", "citations_ok"):
        add("structured", key, _rate(data, key))
    add("structured", "doc_correct_on_combined", _rate([r for r in data if r["kind"] == "both"], "doc_correct"))
    scored = [r for r in rows if r.get("ragas")]
    for m in RAGAS_METRICS:
        add("ragas", m, _mean([r["ragas"].get(m) for r in scored]))
    lat = [r["latency_ms"] for r in rows]
    if lat:
        out.append({"bu": bu, "suite": "golden", "metric": "median_latency_ms", "value": int(statistics.median(lat)),
                    "n": len(lat)})  # fmt: skip
    return out


THRESHOLDED = {  # (suite, metric) -> threshold key; applied to the "all" rows only (per-BU rows are the trend)
    ("golden", f"hit@{K}"): f"hit@{K}",
    ("golden", "answer_correct"): "answer_correct",
    ("golden", "citation_correct"): "citation_correct",
    ("golden", "abstain_correct"): "abstain_correct",
    ("structured", "data_correct"): "data_correct",
    ("ragas", "faithfulness"): "faithfulness",
    ("ragas", "context_recall"): "context_recall",
    ("leak", "leaks"): "leaks",
}


def apply_thresholds(results: list[dict[str, Any]], thresholds: dict[str, Any]) -> list[str]:
    failures = []
    for r in results:
        key = THRESHOLDED.get((r["suite"], r["metric"])) if r["bu"] == "all" else None
        if key is None or key not in thresholds:
            continue
        t = float(thresholds[key])
        r["threshold"] = t
        r["passed"] = r["value"] <= t if r["metric"] == "leaks" else r["value"] >= t
        if not r["passed"]:
            failures.append(f"{r['suite']}.{r['metric']} = {r['value']} (threshold {t})")
    return failures


# ------------------------------------------------------------------------------------------------ report
def git_commit() -> str:
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], capture_output=True, text=True, cwd=REPO_ROOT, check=False).stdout.strip()  # noqa: S603, S607

    sha = git("rev-parse", "--short", "HEAD") or "unknown"
    return sha + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


CSS = (
    "body{font:14px system-ui,sans-serif;margin:24px;color:#1d2433}"
    "table{border-collapse:collapse;width:100%;margin:12px 0}"
    "td,th{border-bottom:1px solid #dfe3ea;padding:4px 8px;text-align:left;vertical-align:top}"
    "th{color:#6b7385;font-size:12px}"
    "tr.ok td:first-child{border-left:3px solid #1d7a46}tr.bad td:first-child{border-left:3px solid #b3261e}"
    ".status{font-size:20px;font-weight:700}"
)


def render_html(report: dict[str, Any]) -> str:
    e = html.escape
    rows = sorted(report["results"], key=lambda r: (r["suite"], r["metric"], r["bu"] != "all", r["bu"]))
    res = "".join(
        f"<tr class='{'' if r.get('passed') is None else ('ok' if r['passed'] else 'bad')}'><td>{e(r['suite'])}</td>"
        f"<td>{e(r['metric'])}</td><td>{e(r['bu'])}</td><td>{r['value']}</td><td>{r['n']}</td>"
        f"<td>{'' if r.get('threshold') is None else r['threshold']}</td></tr>"
        for r in rows
    )
    qs = "".join(
        f"<tr class='{'ok' if q.get('correct', q.get('data_correct')) else 'bad'}'><td>{e(q['bu'])}</td>"
        f"<td>{e(q['id'])}</td><td>{e(q['kind'])}</td><td>{e(q['question'])}</td><td>{e(q['answer'][:300])}</td>"
        f"<td>{e(json.dumps(q.get('ragas') or {}))}</td></tr>"
        for q in report["questions"]
    )
    s = report["run"]
    fails = "".join(f"<li>{e(f)}</li>" for f in s["summary"]["failures"]) or "<li>none</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>KB eval {e(s["run_id"][:8])}</title>
<style>{CSS}.status{{color:{"#1d7a46" if s["status"] == "PASS" else "#b3261e"}}}</style></head><body>
<h1>Knowledge base evaluation</h1><p class="status">{s["status"]}</p>
<p>run {e(s["run_id"])} · commit {e(s["git_commit"])} · {e(s["started_at"])} → {e(s["finished_at"])}</p>
<p>LLM {e(str(s["config"]["llm"]))} · judge {e(str(s["config"]["judge"]))} · expansion {s["config"]["expansion"]}</p>
<h2>Threshold failures</h2><ul>{fails}</ul>
<h2>Metrics</h2><table><tr><th>suite</th><th>metric</th><th>BU</th><th>value</th><th>n</th><th>threshold</th></tr>{res}</table>
<h2>Leak suite</h2><p>{report["leak"].get("tests", 0)} tests, {report["leak"].get("leaks", "not run")} leaks
{e(", ".join(report["leak"].get("failed", [])))}</p>
<h2>Questions</h2><table><tr><th>BU</th><th>id</th><th>kind</th><th>question</th><th>answer</th><th>RAGAS</th></tr>{qs}</table>
</body></html>
"""


def record(settings: Settings, run: dict[str, Any], results: list[dict[str, Any]]) -> None:
    reg = PostgresRegistry(settings.processing["registry_dsn"])  # pipeline role (kb_pipe)
    with reg.transaction("svc:eval") as tx:
        tx.execute("SELECT kb.record_eval_run(%s, %s)", (Jsonb(run), Jsonb(results)))


# ------------------------------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    import duckdb

    ap = argparse.ArgumentParser(prog="kb.evaluation.run")
    ap.add_argument("--skip-ragas", action="store_true")
    ap.add_argument("--skip-leak", action="store_true")
    ap.add_argument("--no-record", action="store_true", help="do not write kb.eval_run (dry runs)")
    ap.add_argument("--bu", action="append", choices=BUS, help="limit to these BUs (repeatable)")
    a = ap.parse_args(argv)
    settings = load_settings(strict=True)
    ev = settings.evaluation
    run_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    workdir = REPO_ROOT / ".infra" / "eval" / stamp
    workdir.mkdir(parents=True, exist_ok=True)

    svc, idp = build_service(settings)
    con = duckdb.connect(str(settings.path("data_dir") / "marts" / "marts.duckdb"), read_only=True)

    def truth(sql: str) -> Any:
        return con.execute(sql).fetchone()[0]  # type: ignore[index]

    questions: list[dict[str, Any]] = []
    for bu in a.bu or BUS:
        b = load_bu(bu)
        ident = idp.resolve(b["user"])
        for q in b["questions"]:
            row = ask_one(svc, ident, q, truth) | {"bu": bu}
            questions.append(row)
            ok = row.get("correct", row.get("data_correct"))
            print(f"{bu:<9} {q['id']:<4} {q['kind']:<9} ok={ok!s:<5} {row['latency_ms']:>6} ms  {row['answer'][:80]!r}",
                  flush=True)  # fmt: skip
    con.close()

    ragas_info: dict[str, Any] = {"skipped": a.skip_ragas}
    if not a.skip_ragas:
        samples = [
            {"id": f"{q['bu']}:{q['id']}", "user_input": q["question"], "response": q["answer"],
             "retrieved_contexts": q["contexts"], "reference": q["reference"]}
            for q in questions
            if q["kind"] in ("documents", "expansion") and q["contexts"] and q["answer"] not in ABSTAINED
        ]  # fmt: skip
        print(f"RAGAS: scoring {len(samples)} answers with {ev['judge_model']} ...", flush=True)
        t0 = time.monotonic()
        rr = ragas_score(samples, settings, workdir / "ragas")
        for q in questions:
            q["ragas"] = rr.scores.get(f"{q['bu']}:{q['id']}")
        ragas_info = {"samples": len(samples), "errors": rr.errors, "blocked_attempts": len(rr.blocked_attempts),
                      "seconds": int(time.monotonic() - t0)}  # fmt: skip

    leak: dict[str, Any] = {"skipped": True}
    if not a.skip_leak:
        print("leak suite: retrieval matrix + real-LLM matrix ...", flush=True)
        leak = run_leak_suite(workdir)

    results: list[dict[str, Any]] = []
    for bu in a.bu or BUS:
        results += metrics_for([q for q in questions if q["bu"] == bu], bu)
    results += metrics_for(questions, "all")
    if not leak.get("skipped"):
        results.append({"bu": "all", "suite": "leak", "metric": "leaks", "value": leak["leaks"], "n": leak["tests"]})
    failures = apply_thresholds(results, ev.get("thresholds", {}))
    skipped = [s for s, on in (("ragas", a.skip_ragas), ("leak", a.skip_leak)) if on] + (["bu-subset"] if a.bu else [])
    status = "PASS" if not failures and not skipped else "FAIL"
    if skipped and not failures:
        failures.append(f"incomplete run: skipped {', '.join(skipped)}")
    finished = datetime.now(UTC)
    run = {
        "run_id": run_id,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        "status": status,
        "config": {
            "llm": settings.ports["llm"].options.get("model"),
            "judge": ev.get("judge_model"),
            "embedder": settings.ports["embedder"].options.get("model"),
            "reranker": settings.ports["reranker"].options.get("model"),
            "expansion": bool(settings.graph.get("expansion")),
            "thresholds": ev.get("thresholds", {}),
            "bus": list(a.bu or BUS),
        },
        "summary": {
            "failures": failures,
            "questions": len(questions),
            "leaks": leak.get("leaks"),
            "ragas": {k: v for k, v in ragas_info.items() if k != "errors"},
        },
    }
    report_dir = REPO_ROOT / str(ev.get("report_dir", "docs/eval"))
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json = report_dir / f"report-{stamp}.json"
    run["report_uri"] = str(report_json.relative_to(REPO_ROOT)).replace("\\", "/")
    for q in questions:
        q.pop("contexts", None)  # chunk text stays out of the committed report (ids/answers suffice)
    report = {"run": run, "results": results, "questions": questions, "leak": leak, "ragas": ragas_info}
    report_json.write_bytes((json.dumps(report, indent=1, default=str) + "\n").encode("utf-8"))  # LF on Windows
    report_json.with_suffix(".html").write_bytes(render_html(report).encode("utf-8"))
    if not a.no_record:
        record(settings, run, results)
    print(json.dumps({"status": status, "failures": failures, "report": run["report_uri"]}, indent=1))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
