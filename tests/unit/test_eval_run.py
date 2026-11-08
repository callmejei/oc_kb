"""`make eval` scoring logic without the live stack: aggregation, thresholds, fail-closed partial runs, report."""

from __future__ import annotations

from kb.evaluation.run import apply_thresholds, metrics_for, render_html

ROWS = [
    {"kind": "documents", "hit_at_k": True, "correct": True, "citation_correct": True, "latency_ms": 100,
     "ragas": {"faithfulness": 1.0, "answer_relevancy": 0.9, "context_precision": 1.0, "context_recall": 1.0}},
    {"kind": "documents", "hit_at_k": True, "correct": False, "citation_correct": True, "latency_ms": 300,
     "ragas": {"faithfulness": 0.5, "answer_relevancy": None, "context_precision": 1.0, "context_recall": 0.0}},
    {"kind": "expansion", "hit_at_k": False, "correct": False, "citation_correct": False, "latency_ms": 200},
    {"kind": "abstain", "correct": True, "latency_ms": 50},
    {"kind": "data", "data_correct": True, "route_correct": True, "citations_ok": True, "doc_correct": None,
     "latency_ms": 90},
    {"kind": "both", "data_correct": False, "route_correct": True, "citations_ok": False, "doc_correct": True,
     "latency_ms": 400},
]  # fmt: skip


def by(results, suite, metric):  # type: ignore[no-untyped-def]
    return next(r for r in results if r["suite"] == suite and r["metric"] == metric)


def test_metrics_per_kind_and_ragas_means_skip_missing_scores():
    m = metrics_for(ROWS, "all")
    assert by(m, "golden", "answer_correct") == {"bu": "all", "suite": "golden", "metric": "answer_correct",
                                                  "value": 0.5, "n": 2}  # fmt: skip
    assert by(m, "expansion", "hit@5")["value"] == 0.0
    assert by(m, "golden", "abstain_correct")["value"] == 1.0
    assert by(m, "structured", "data_correct")["value"] == 0.5 and by(m, "structured", "data_correct")["n"] == 2
    assert by(m, "structured", "doc_correct_on_combined")["value"] == 1.0
    assert by(m, "ragas", "faithfulness")["value"] == 0.75
    assert by(m, "ragas", "answer_relevancy") == {"bu": "all", "suite": "ragas", "metric": "answer_relevancy",
                                                  "value": 0.9, "n": 1}  # fmt: skip
    assert by(m, "golden", "median_latency_ms")["value"] == 150


def test_thresholds_apply_to_overall_rows_only_and_leaks_must_be_zero():
    results = metrics_for(ROWS, "retail") + metrics_for(ROWS, "all")
    results.append({"bu": "all", "suite": "leak", "metric": "leaks", "value": 1, "n": 40})
    th = {"answer_correct": 0.8, "faithfulness": 0.7, "abstain_correct": 0.75, "leaks": 0}
    failures = apply_thresholds(results, th)
    assert sorted(failures) == [
        "golden.answer_correct = 0.5 (threshold 0.8)",
        "leak.leaks = 1 (threshold 0.0)",
    ]
    retail = [r for r in results if r["bu"] == "retail"]
    assert all("passed" not in r for r in retail)  # per-BU rows are trend data, not gates
    assert by([r for r in results if r["bu"] == "all"], "golden", "abstain_correct")["passed"] is True


def test_report_is_self_contained_and_escapes_text():
    report = {
        "run": {"run_id": "r" * 36, "git_commit": "abc", "started_at": "t0", "finished_at": "t1", "status": "FAIL",
                "config": {"llm": "qwen3:8b", "judge": "llama3.1:8b", "expansion": True},
                "summary": {"failures": ["golden.answer_correct = 0.5 (threshold 0.8)"]}},
        "results": metrics_for(ROWS, "all"),
        "questions": [{"bu": "retail", "id": "r1", "kind": "documents", "question": "<script>x</script>",
                       "answer": "a & b", "correct": True}],
        "leak": {"tests": 40, "leaks": 0, "failed": []},
    }  # fmt: skip
    page = render_html(report)
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page and "a &amp; b" in page
    assert "http" not in page.split("<body>")[0]  # no external assets
    assert "golden.answer_correct = 0.5" in page and ">FAIL<" in page
