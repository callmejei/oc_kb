"""Semantic model & products (Phase 9c/9d, D-067), mounted at /api/v1/semantic.

- ``GET /metrics``: the semantic catalogue for ANY user, limited to published metrics on tables the caller is granted
  (a metric on a table you cannot query is not shown, so its name and definition do not leak), with lineage
  (metric -> table -> data product) and the table's latest data-quality status.
- Governance (makers/checkers of the `data` domain): drafts, submit, checker decisions with a definition diff,
  single-actor retire. The database enforces four-eyes, locking, stale approvals and the no-masked-columns rule.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from kb.gateway.deps import GatewayState, current_identity, db_errors, roles, state
from kb.gateway.routes_ops import OPS_GROUP
from kb.gateway.routes_products import DOMAIN, governor
from kb.ports.types import Identity
from kb.semantic import governance as repo
from kb.semantic.model import MetricError, compile_metric, validate

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]
Gov = Annotated[Identity, Depends(governor)]


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    description: str = Field(min_length=1, max_length=2000)
    definition: dict[str, Any]


class Comment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=2000)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    reason: str | None = Field(default=None, max_length=2000)


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=2000)


def _engine(st: GatewayState) -> Any:
    t2s = getattr(st.qa(), "t2s", None)
    if t2s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "structured data is not configured")
    return t2s.engine


def _rows(st: GatewayState, ident: Identity, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with st.registry.transaction(ident.user_id) as tx:
        return list(tx.execute(sql, params).fetchall())


# ------------------------------------------------------------------------------------------------ catalogue
@router.get("/metrics")
def catalogue(ident: Ident, st: St) -> dict[str, Any]:
    eng = _engine(st)
    granted = set(eng._policy.catalog) if OPS_GROUP in ident.groups else set(eng.policy.granted_tables(ident))
    rows = _rows(
        st,
        ident,
        """SELECT m.metric_id, m.product_id, m.revision_id, m.revision_no, m.definition, m.description,
                  pr.owner, pr.classification, pr.revision_no AS product_revision
             FROM kb.metric_current m
             JOIN kb.data_product p ON p.product_id = m.product_id
             JOIN kb.product_revision pr ON pr.revision_id = p.current_revision_id
            ORDER BY m.metric_id""",
    )
    dq = {
        r["table_name"]: r
        for r in _rows(
            st,
            ident,
            """SELECT table_name, count(*) FILTER (WHERE passed) AS passed, count(*) AS rules
                 FROM kb.dq_result WHERE run_id = (SELECT run_id FROM kb.dq_run ORDER BY started_at DESC LIMIT 1)
                GROUP BY table_name""",
        )
    }
    out = []
    for r in rows:
        if r["product_id"] not in granted:
            continue
        d = r["definition"]
        masked = set(eng.policy.masked_columns(ident, d["table"])) if OPS_GROUP not in ident.groups else set()
        out.append(
            {
                "metric_id": r["metric_id"],
                "label": d["label"],
                "synonyms": d.get("synonyms", []),
                "description": r["description"],
                "unit": d["unit"],
                "dimensions": d["dimensions"],
                "concept": d.get("concept") or None,
                "revision": r["revision_no"],
                "sql": compile_metric(d),
                "lineage": {
                    "table": d["table"],
                    "columns": d["columns"],
                    "product_revision": r["product_revision"],
                    "owner": r["owner"],
                    "classification": r["classification"],
                },
                "quality": dq.get(d["table"]),
                "usable": not (set(d["columns"]) & masked),  # a mask added later stops it for the masked
            }
        )
    return {"metrics": out, "not_shown": max(0, len(rows) - len(out))}


# ------------------------------------------------------------------------------------------------ governance
@router.get("/governance")
def governance_view(ident: Gov, st: St) -> dict[str, Any]:
    metrics = _rows(
        st,
        ident,
        """SELECT m.metric_id, m.product_id, m.status, r.revision_no, r.definition, r.description
             FROM kb.metric m LEFT JOIN kb.metric_revision r ON r.revision_id = m.current_revision_id
            ORDER BY m.metric_id""",
    )
    requests = _rows(
        st,
        ident,
        """SELECT q.request_id, q.request_type, q.metric_id, q.maker_id, q.maker_comment, q.submitted_at, q.status,
                  r.revision_no, r.definition AS proposed, r.description, cur.definition AS current,
                  kb.metric_masked_columns(r.definition) AS masked_columns,
                  (r.base_revision_id IS DISTINCT FROM m.current_revision_id) AS stale
             FROM kb.metric_request q JOIN kb.metric m ON m.metric_id = q.metric_id
             LEFT JOIN kb.metric_revision r ON r.revision_id = q.revision_id
             LEFT JOIN kb.metric_revision cur ON cur.revision_id = m.current_revision_id
            WHERE q.status = 'PENDING' ORDER BY q.submitted_at DESC""",
    )
    for r in requests:
        r["own"] = r["maker_id"] == ident.user_id
        r["sql"] = compile_metric(r["proposed"]) if r["proposed"] else None
    drafts = _rows(
        st,
        ident,
        """SELECT revision_id, metric_id, revision_no, definition, description FROM kb.metric_revision
            WHERE status = 'DRAFT' AND created_by = %s ORDER BY created_at DESC""",
        (ident.user_id,),
    )
    eng = _engine(st)
    return {
        "metrics": metrics,
        "requests": requests,
        "drafts": drafts,
        "maker": DOMAIN in roles(st, ident, "MAKER"),
        "checker": DOMAIN in roles(st, ident, "CHECKER"),
        "columns": {t: list(tp.columns) for t, tp in eng._policy.catalog.items()},
    }


@router.post("/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(body: Draft, ident: Gov, st: St) -> dict[str, Any]:
    cols = {t: tp.columns for t, tp in _engine(st)._policy.catalog.items()}
    table = str(body.definition.get("table", ""))
    if table not in cols:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{table or '(none)'} is not a mart table")
    try:
        clean = validate(body.definition, cols[table])
    except (MetricError, KeyError, ValueError, TypeError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid metric: {e}") from None
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        rev = repo.draft(tx, body.metric_id, clean, body.description, cols[table])
    return {"revision_id": rev, "sql": compile_metric(clean)}


@router.delete("/drafts/{revision_id}")
def discard(revision_id: str, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        tx.execute("SELECT kb.discard_metric_draft(%s)", (revision_id,))
    return {"discarded": revision_id}


@router.post("/drafts/{revision_id}/submit", status_code=status.HTTP_201_CREATED)
def submit(revision_id: str, body: Comment, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        return {"request_id": repo.submit(tx, revision_id, body.comment)}


@router.post("/requests/{request_id}/decide")
def decide(request_id: str, body: Decision, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        repo.decide(tx, request_id, body.approve, body.reason)
    return {"request_id": request_id, "status": "APPROVED" if body.approve else "REJECTED"}


@router.post("/requests/{request_id}/withdraw")
def withdraw(request_id: str, body: Comment, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        tx.execute("SELECT kb.withdraw_metric_request(%s, %s)", (request_id, body.comment))
    return {"withdrawn": request_id}


@router.post("/{metric_id}/retire", status_code=status.HTTP_201_CREATED)
def retire(metric_id: str, body: Reason, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        return {"request_id": repo.retire(tx, metric_id, body.reason), "status": "EXECUTED"}
