"""Data & knowledge quality (Phase 9a, D-065), mounted at /api/v1/quality.

- Data: the latest recorded data-quality run (kb.dq_result: counts only). A caller sees rules only for tables they
  are granted; members of GRP_KB_OPS see every rule. Counts are whole-table (quality is measured on all rows),
  the same disclosure as the catalogue's table row count.
- Knowledge: corpus health per BU from the registry (current documents, open exceptions by reason, labelled
  problems on current versions, stale documents, ontology review backlog), for the caller's readable BUs only
  (ops: all BUs). Aggregates only; no titles or ids.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from kb.gateway.deps import GatewayState, current_identity, state
from kb.gateway.routes_ops import OPS_GROUP
from kb.ports.types import Identity

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]
STALE_DAYS = 365


def _rows(st: GatewayState, ident: Identity, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with st.registry.transaction(ident.user_id) as tx:
        return list(tx.execute(sql, params).fetchall())


def _granted(st: GatewayState, ident: Identity) -> set[str] | None:
    """Tables whose quality the caller may see; None = all (ops)."""
    if OPS_GROUP in ident.groups:
        return None
    t2s = getattr(st.qa(), "t2s", None)
    return set(t2s.engine.policy.granted_tables(ident)) if t2s is not None else set()


@router.get("")
def quality(ident: Ident, st: St) -> dict[str, Any]:
    ops = OPS_GROUP in ident.groups
    granted = _granted(st, ident)
    latest = "SELECT run_id, started_at, finished_at, as_of FROM kb.dq_run ORDER BY started_at DESC LIMIT 1"
    run = _rows(st, ident, latest)
    rules: list[dict[str, Any]] = []
    if run and (granted is None or granted):
        rules = [
            r
            for r in _rows(
                st,
                ident,
                """SELECT rule_id, table_name, rule_kind, column_name, checked, failed, tolerance, passed, error
                     FROM kb.dq_result WHERE run_id = %s ORDER BY table_name, rule_id""",
                (run[0]["run_id"],),
            )
            if granted is None or r["table_name"] in granted
        ]
    bus = None if ops else sorted(st.idp.readable_bus(ident))
    knowledge = _knowledge(st, ident, bus) if (bus is None or bus) else []
    return {
        "data": {
            "run": run[0] if run and rules else None,
            "rules": rules,
            "tables_not_shown": None if granted is None else max(0, len(_all_tables(st)) - len(granted)),
        },
        "knowledge": knowledge,
        "scope": "all" if ops else "own",
        "stale_days": STALE_DAYS,
    }


def _all_tables(st: GatewayState) -> list[str]:
    t2s = getattr(st.qa(), "t2s", None)
    return list(t2s.engine.policy.catalog) if t2s is not None else []


def _knowledge(st: GatewayState, ident: Identity, bus: list[str] | None) -> list[dict[str, Any]]:
    flt = "" if bus is None else "WHERE s.bu = ANY(%s)"
    params: tuple[Any, ...] = () if bus is None else (bus,)
    return _rows(
        st,
        ident,
        f"""
        WITH s AS (SELECT source_id, bu FROM kb.kb_source)
        SELECT b.bu,
          (SELECT count(*) FROM kb.document d JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current
             JOIN s ON s.source_id = d.source_id
            WHERE s.bu = b.bu AND d.status = 'ACTIVE' AND v.status = 'INDEXED')            AS current_documents,
          (SELECT count(*) FROM kb.document d JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current
             JOIN s ON s.source_id = d.source_id
            WHERE s.bu = b.bu AND d.status = 'ACTIVE' AND v.status = 'INDEXED'
              AND v.created_at < now() - make_interval(days => %s))                     AS stale_documents,
          (SELECT coalesce(jsonb_object_agg(reason, n), '{{}}'::jsonb) FROM (
             SELECT e.reason, count(*) AS n FROM kb.exception_item e JOIN s ON s.source_id = e.source_id
              WHERE s.bu = b.bu AND e.status = 'OPEN' GROUP BY e.reason) x)               AS open_exceptions,
          (SELECT count(DISTINCT l.doc_id) FROM kb.inspection_label l
             JOIN kb.document_version v ON v.version_id = l.version_id AND v.is_current
             JOIN kb.document d ON d.doc_id = l.doc_id JOIN s ON s.source_id = d.source_id
            WHERE s.bu = b.bu)                                                            AS labelled_documents,
          (SELECT count(*) FROM kb.concept_review c WHERE c.bu = b.bu AND c.status = 'PENDING') AS concepts_pending,
          (SELECT count(*) FROM kb.concept_review c WHERE c.bu = b.bu AND c.status = 'ACCEPTED') AS concepts_accepted
        FROM (SELECT DISTINCT s.bu FROM s {flt}) b
        ORDER BY b.bu""",  # noqa: S608 - fixed SQL; only the optional BU filter clause is chosen, values are bound
        (STALE_DAYS, *params),
    )
