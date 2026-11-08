"""Explorer shell API (Phase 8, mounted at /api/v1/explorer): read-only views that the explorer UI screens are wired to.

Every endpoint is scoped to the caller, with the same authorities as the rest of the gateway:

- ``POST /search``: retrieval only (no LLM), through the mandatory access pre-filter, audited like a question
  (action SEARCH; the text goes to ``kb.query_log`` only, the audit row holds a hash and chunk ids).
- ``GET /spaces/inventory``: documents the caller can see, per BU space. The chunk index decides (same filter
  as retrieval), so a private upload of someone else or a non-current version never appears.
- ``GET /catalog/marts``: only tables the caller is granted, with the caller's own masks and row filters.
- ``GET /catalog/table-map``: concept -> mart entries for granted tables only (read-only; D-061).
- ``GET /catalog/graphs``: knowledge-graph counts for the caller's readable BU graphs only.
- ``GET /governance/my-access``: the caller's own groups, roles, spaces, table grants, masks, row filters.
- ``GET /governance/activity``: the caller's own audit rows (metadata; no details payload).
- ``GET /governance/approvals``: approval history of sources in BUs where the caller is maker or checker.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from kb.gateway.acl import build_access_filter, describe
from kb.gateway.deps import GatewayState, current_identity, roles, state
from kb.gateway.routes_ontology import overlay
from kb.ports.types import Identity

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]
SNIPPET = 320
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{3,}")


def snippet(text: str, query: str) -> str:
    """~SNIPPET chars of the chunk, starting just before the first query term it contains (else the start)."""
    low = text.lower()
    hits = [i for w in _WORD.findall(query) if (i := low.find(w.lower())) >= 0]
    start = max(0, min(hits) - 80) if hits else 0
    out = text[start : start + SNIPPET]
    return ("…" if start else "") + out + ("…" if start + SNIPPET < len(text) else "")


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=500)
    spaces: list[str] | None = Field(default=None, max_length=10)  # narrows only


def _t2s(st: GatewayState) -> Any:
    t2s = getattr(st.qa(), "t2s", None)
    if t2s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "structured data is not configured")
    return t2s


# ------------------------------------------------------------------------------------------------ search
@router.post("/search")
def search(body: SearchBody, ident: Ident, st: St) -> dict[str, Any]:
    qa = st.qa()
    flt = build_access_filter(ident, body.spaces)
    scope = qa.scope_fn(ident) if getattr(qa, "scope_fn", None) else None
    if scope is not None and body.spaces:
        scope = [b for b in scope if b in body.spaces]
    ret = qa.retriever.retrieve(body.query, flt, scope=scope)
    results = []
    for h in ret.context:
        c = h.chunk
        results.append(
            {
                "chunk_id": c.chunk_id,
                "doc_id": c.doc_id,
                "version_id": c.version_id,
                "version_no": c.metadata.get("version_no"),
                "title": c.metadata.get("title"),
                "bu": c.bu,
                "section": c.section_path,
                "page": c.page,
                "snippet": snippet(c.text, body.query),
                "score": round(h.score, 4),
            }
        )
    query_id = str(uuid.uuid4())
    details = {
        "groups": sorted(ident.groups),
        "filter": describe(flt),
        "graph_scope": scope,
        "retrieved_chunk_ids": ret.retrieved_ids,
        "result_chunk_ids": [r["chunk_id"] for r in results],
        "question_sha256": hashlib.sha256(body.query.encode()).hexdigest(),
    }
    with st.registry.transaction(ident.user_id) as tx:
        tx.execute(
            "INSERT INTO kb.query_log (query_id, user_id, question) VALUES (%s, kb.current_actor(), %s)",
            (query_id, body.query),
        )
        row = tx.execute(
            """INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, details)
               VALUES (kb.current_actor(), 'SEARCH', 'query', %s, %s) RETURNING audit_id""",
            (query_id, Jsonb(details)),
        ).fetchone()
    assert row is not None
    return {"query_id": query_id, "audit_id": int(row["audit_id"]), "results": results}


# ------------------------------------------------------------------------------------------------ spaces
@router.get("/spaces/inventory")
def inventory(ident: Ident, st: St) -> dict[str, Any]:
    bus = sorted(st.idp.readable_bus(ident))
    docs = list(st.chunk_index.visible_documents(build_access_filter(ident))) if ident.groups else []
    other = [d for d in docs if d.get("bu") not in bus]  # e.g. the caller's own private upload elsewhere
    spaces = []
    for bu in bus:
        mine = [d for d in docs if d.get("bu") == bu]
        spaces.append({"bu": bu, "documents": mine, "count": len(mine)})
    return {"bus": bus, "spaces": spaces, "other_visible": other}


# ------------------------------------------------------------------------------------------------ catalog
@router.get("/catalog/marts")
def marts(ident: Ident, st: St) -> dict[str, Any]:
    engine = _t2s(st).engine
    pol = engine.policy
    tables = []
    for t in pol.granted_tables(ident):
        meta = pol.descriptions.get(t, {})
        masked = pol.masked_columns(ident, t)
        tables.append(
            {
                "table": t,
                "description": meta.get("description", ""),
                "rows": pol.catalog[t].rows,
                "row_filter": pol.row_filter(ident, t),
                "columns": [
                    {"name": c, "description": meta.get("columns", {}).get(c, ""), "mask": masked.get(c)}
                    for c in pol.catalog[t].columns
                ],
            }
        )
    return {"tables": tables, "not_granted": len(pol.catalog) - len(tables)}


@router.get("/catalog/table-map")
def table_map(ident: Ident, st: St) -> dict[str, Any]:
    t2s = _t2s(st)
    tm = getattr(t2s, "table_map", None)
    if tm is None:
        return {"entries": [], "hidden": 0}
    granted = set(t2s.engine.policy.granted_tables(ident))
    entries, hidden = [], 0
    for iri, e in sorted(tm.entries.items(), key=lambda kv: (kv[1]["table"], kv[0])):
        if e["table"] not in granted:
            hidden += 1
            continue
        c = tm.o.concepts.get(iri)
        entries.append(
            {
                "concept": iri,
                "curie": c.curie if c else iri,
                "label": c.label if c else iri,
                "table": e["table"],
                "hint": e["hint"],
            }
        )
    return {"entries": entries, "hidden": hidden, "source": "config/table_map.yaml", "editable": False}


@router.get("/catalog/graphs")
def graphs(ident: Ident, st: St) -> dict[str, Any]:
    ov = overlay(ident, st)
    per: dict[str, dict[str, int]] = {b: {"entities": 0, "chunks": 0} for b in ov["bus"]}
    for e in ov["entities"]:
        p = per[e["bu"]]
        p["entities"] += 1
        p["chunks"] += e["chunks"]
        p[e["match"]] = p.get(e["match"], 0) + 1
    return {"bus": ov["bus"], "graphs": [{"bu": b, **v} for b, v in per.items()], "counts": ov["counts"]}


# ------------------------------------------------------------------------------------------------ governance
@router.get("/governance/my-access")
def my_access(ident: Ident, st: St) -> dict[str, Any]:
    out: dict[str, Any] = {
        "user_id": ident.user_id,
        "groups": sorted(ident.groups),
        "readable_bus": sorted(st.idp.readable_bus(ident)),
        "maker_bus": roles(st, ident, "MAKER"),
        "checker_bus": roles(st, ident, "CHECKER"),
        "tables": [],
    }
    t2s = getattr(st.qa(), "t2s", None)
    if t2s is not None:
        pol = t2s.engine.policy
        out["tables"] = [
            {"table": t, "row_filter": pol.row_filter(ident, t), "masked": pol.masked_columns(ident, t)}
            for t in pol.granted_tables(ident)
        ]
    return out


@router.get("/governance/activity")
def activity(ident: Ident, st: St, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:
    with st.registry.transaction(ident.user_id) as tx:
        return list(
            tx.execute(
                """SELECT audit_id, ts, action, object_type, object_id FROM kb.audit_log
                    WHERE actor_id = %s ORDER BY audit_id DESC LIMIT %s""",
                (ident.user_id, limit),
            ).fetchall()
        )


@router.get("/governance/approvals")
def approvals(ident: Ident, st: St, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    bus = sorted(set(roles(st, ident, "MAKER")) | set(roles(st, ident, "CHECKER")))
    if not bus:
        return {"bus": [], "requests": []}
    with st.registry.transaction(ident.user_id) as tx:
        rows = tx.execute(
            """SELECT r.request_id, r.request_type, r.source_id, s.bu, s.display_name, r.maker_id, r.submitted_at,
                      r.status, r.checker_id, r.decided_at, r.decision_reason
                 FROM kb.approval_request r JOIN kb.kb_source s ON s.source_id = r.source_id
                WHERE s.bu = ANY(%s) ORDER BY r.submitted_at DESC LIMIT %s""",
            (bus, limit),
        ).fetchall()
    return {"bus": bus, "requests": list(rows)}
