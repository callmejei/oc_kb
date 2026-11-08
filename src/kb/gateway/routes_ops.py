"""Operations dashboard API (Phase 7): ingestion health, exception backlog, leak-suite status, answer-quality
trend, open answer flags.

Members of the ops group only (``OPS_GROUP``; D-058). Everything here is counts and metadata from the
registry: source names, run states, exception reasons, metric values. No document text, no question text,
no chunk ids. Being in the ops group grants no document access (readable BUs come from READ/MAKER/CHECKER
groups only), so the dashboard cannot become a side door to content.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kb.config import REPO_ROOT
from kb.gateway.deps import GatewayState, current_identity, state
from kb.ports.types import Identity

OPS_GROUP = "GRP_KB_OPS"

router = APIRouter()
St = Annotated[GatewayState, Depends(state)]


def ops_identity(identity: Annotated[Identity, Depends(current_identity)]) -> Identity:
    if OPS_GROUP not in identity.groups:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"the operations dashboard requires {OPS_GROUP}")
    return identity


Ops = Annotated[Identity, Depends(ops_identity)]


def _rows(st: GatewayState, ident: Identity, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with st.registry.transaction(ident.user_id) as tx:
        return list(tx.execute(sql, params).fetchall())


@router.get("/ingestion")
def ingestion(st: St, ident: Ops) -> list[dict[str, Any]]:
    """Per source: status, last run, last success, hours since last success, indexed docs, open exceptions."""
    return _rows(
        st,
        ident,
        """SELECT s.source_id, s.bu, s.display_name, s.source_type, s.status,
                  lr.run_kind AS last_run_kind, lr.status AS last_run_status, lr.finished_at AS last_run_finished_at,
                  ss.last_success_at,
                  round(extract(epoch FROM now() - ss.last_success_at) / 3600.0, 1) AS hours_since_success,
                  (SELECT count(*) FROM kb.ingestion_run r WHERE r.source_id = s.source_id AND r.status = 'FAILED'
                     AND r.created_at > now() - interval '7 days') AS failed_runs_7d,
                  (SELECT count(*) FROM kb.document d JOIN kb.document_version v
                     ON v.doc_id = d.doc_id AND v.is_current AND v.status = 'INDEXED'
                   WHERE d.source_id = s.source_id AND d.status = 'ACTIVE') AS indexed_docs,
                  (SELECT count(*) FROM kb.exception_item e WHERE e.source_id = s.source_id AND e.status = 'OPEN')
                     AS open_exceptions
             FROM kb.kb_source s
             LEFT JOIN kb.sync_state ss ON ss.source_id = s.source_id
             LEFT JOIN LATERAL (SELECT run_kind, status, finished_at FROM kb.ingestion_run r
                                 WHERE r.source_id = s.source_id ORDER BY r.created_at DESC LIMIT 1) lr ON true
            WHERE s.status <> 'RETIRED'
            ORDER BY s.bu, s.source_id""",
    )


@router.get("/exceptions")
def exceptions(st: St, ident: Ops) -> dict[str, Any]:
    """Open exception backlog by BU and reason, with age buckets."""
    rows = _rows(
        st,
        ident,
        """SELECT s.bu, e.reason, count(*) AS open,
                  count(*) FILTER (WHERE e.created_at > now() - interval '1 day') AS under_1d,
                  count(*) FILTER (WHERE e.created_at <= now() - interval '1 day'
                                     AND e.created_at > now() - interval '7 days') AS d1_to_7,
                  count(*) FILTER (WHERE e.created_at <= now() - interval '7 days') AS over_7d,
                  min(e.created_at) AS oldest
             FROM kb.exception_item e JOIN kb.kb_source s ON s.source_id = e.source_id
            WHERE e.status = 'OPEN'
            GROUP BY s.bu, e.reason ORDER BY s.bu, e.reason""",
    )
    return {"total_open": sum(int(r["open"]) for r in rows), "by_bu_reason": rows}


@router.get("/eval")
def eval_trend(st: St, ident: Ops, limit: Annotated[int, Query(ge=1, le=200)] = 30) -> dict[str, Any]:
    """Recent `make eval` runs (newest first) with their overall metrics, and the leak-suite status."""
    runs = _rows(
        st,
        ident,
        """SELECT run_id, started_at, finished_at, git_commit, status, summary, config->>'llm' AS llm,
                  config->>'judge' AS judge, report_uri
             FROM kb.eval_run ORDER BY started_at DESC LIMIT %s""",
        (limit,),
    )
    ids = [r["run_id"] for r in runs]
    results = (
        _rows(
            st,
            ident,
            """SELECT run_id, bu, suite, metric, value::float AS value, n, threshold::float AS threshold, passed
                 FROM kb.eval_result WHERE run_id = ANY(%s) ORDER BY suite, metric, bu""",
            (ids,),
        )
        if ids
        else []
    )
    by_run: dict[Any, list[dict[str, Any]]] = {}
    for r in results:
        by_run.setdefault(r.pop("run_id"), []).append(r)
    for r in runs:
        r["results"] = by_run.get(r["run_id"], [])
    leak_runs = [r for r in runs if any(x["suite"] == "leak" for x in r["results"])]
    leak = None
    if leak_runs:
        latest = leak_runs[0]
        row = next(x for x in latest["results"] if x["suite"] == "leak")
        leak = {"run_id": latest["run_id"], "at": latest["finished_at"], "leaks": row["value"], "tests": row["n"],
                "passed": row["passed"]}  # fmt: skip
    return {"runs": runs, "leak": leak}


@router.get("/eval/{run_id}/failures")
def eval_failures(run_id: str, st: St, ident: Ops) -> dict[str, Any]:
    """Golden questions a recorded run got wrong (answer, citation or retrieval), from the run's committed report.
    These are the synthetic golden-set questions in tests/eval/golden, never user questions. The report path comes
    from kb.eval_run and must resolve inside docs/eval."""
    rows = _rows(st, ident, "SELECT report_uri FROM kb.eval_run WHERE run_id::text = %s", (run_id,))
    if not rows or not rows[0]["report_uri"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown run or no report")
    base = (REPO_ROOT / "docs" / "eval").resolve()
    path = (REPO_ROOT / rows[0]["report_uri"]).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not available")
    report = json.loads(path.read_text(encoding="utf-8"))
    fails = [
        {k: q.get(k) for k in ("id", "bu", "kind", "question", "route", "correct", "citation_correct", "hit_at_k")}
        for q in report.get("questions", [])
        if q.get("correct") is False or q.get("citation_correct") is False or q.get("hit_at_k") is False
    ]
    return {"run_id": run_id, "questions": len(report.get("questions", [])), "failures": fails}


@router.get("/feedback")
def feedback(st: St, ident: Ops) -> list[dict[str, Any]]:
    """Answer flags per BU: open, resolved, and thumbs-down in the last 7 days (counts only)."""
    return _rows(
        st,
        ident,
        """SELECT t.bu,
                  count(*) FILTER (WHERE t.status = 'OPEN') AS open,
                  count(*) FILTER (WHERE t.status = 'RESOLVED') AS resolved,
                  count(*) FILTER (WHERE f.created_at > now() - interval '7 days') AS last_7d
             FROM kb.feedback_target t JOIN kb.answer_feedback f ON f.feedback_id = t.feedback_id
            GROUP BY t.bu ORDER BY t.bu""",
    )
