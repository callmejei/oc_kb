"""Ontology star map API (Phase 7).

- ``GET /fibo``: the FIBO star map artefact (kb.graph.starmap). FIBO is a public standard with no bank data, so
  any signed-in user may load it.
- ``GET /overlay``: the caller's BU entities attached to FIBO / local concepts, with mapping status
  (exact, accepted, pending review, unmapped). Read ONLY from the named graphs of the caller's readable BUs (the
  same scoping as graph expansion), and every row is re-checked against that set before it is returned.
- ``GET /entity?iri=``: source documents of one entity, resolved through the chunk index WITH the caller's
  mandatory access filter (a private upload or a non-current version stays invisible), plus mart hints from
  ``table_map`` filtered by the caller's table grants.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

from kb.gateway.acl import build_access_filter
from kb.gateway.deps import GatewayState, current_identity, state
from kb.graph.build import V, graph_iri
from kb.graph.starmap import OUT as STARMAP
from kb.ports.types import Identity

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]
SKOS = "http://www.w3.org/2004/02/skos/core#"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
ENTITY_IRI = re.compile(r"https://kb\.local/id/[a-z]+/[0-9a-f]{16}")  # entity_iri() form only


def _graphs(st: GatewayState, ident: Identity) -> tuple[list[str], list[str]]:
    bus = sorted(st.idp.readable_bus(ident))
    return bus, [graph_iri(b) for b in bus]


@router.get("/fibo")
def fibo(_: Ident) -> FileResponse:
    if not STARMAP.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "star map not built: run `python -m kb.graph.starmap`")
    return FileResponse(STARMAP, media_type="application/json", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/overlay")
def overlay(ident: Ident, st: St) -> dict[str, Any]:
    bus, graphs = _graphs(st, ident)
    if not graphs:
        return {"bus": [], "entities": [], "counts": {}}
    rows = st.graph_store().query(
        f"""SELECT ?e ?label ?bu ?concept ?exact ?close (COUNT(DISTINCT ?ch) AS ?chunks) WHERE {{
              ?e a <{V}Entity> ; <{RDFS_LABEL}> ?label ; <{V}bu> ?bu .
              OPTIONAL {{ ?e <{V}concept> ?concept }}
              OPTIONAL {{ ?e <{SKOS}exactMatch> ?exact }}
              OPTIONAL {{ ?e <{SKOS}closeMatch> ?close }}
              OPTIONAL {{ ?e <{V}fromChunk> ?ch }}
            }} GROUP BY ?e ?label ?bu ?concept ?exact ?close""",
        graphs,
    )
    with st.registry.transaction(ident.user_id) as tx:
        pending = {
            r["entity_iri"]: r
            for r in tx.execute(
                """SELECT entity_iri, proposed_concept, confidence::float AS confidence FROM kb.concept_review
                    WHERE status = 'PENDING' AND bu = ANY(%s)""",
                (bus,),
            ).fetchall()
        }
    allowed = set(bus)
    entities = []
    for r in rows:
        if r["bu"] not in allowed:  # belt and braces: the store is already scoped to these graphs
            continue
        p = pending.get(r["e"])
        match = "exact" if r["exact"] else "accepted" if r["close"] else "pending" if p else "unmapped"
        entities.append(
            {
                "iri": r["e"],
                "label": r["label"],
                "bu": r["bu"],
                "concept": r["concept"] or (p["proposed_concept"] if p else None),
                "match": match,
                "confidence": p["confidence"] if p else None,
                "chunks": int(r["chunks"] or 0),
            }
        )
    counts: dict[str, int] = {}
    for e in entities:
        counts[e["match"]] = counts.get(e["match"], 0) + 1
    return {"bus": bus, "entities": sorted(entities, key=lambda e: (e["bu"], e["label"])), "counts": counts}


@router.get("/entity")
def entity(ident: Ident, st: St, iri: Annotated[str, Query(min_length=8, max_length=500)]) -> dict[str, Any]:
    bus, graphs = _graphs(st, ident)
    rows = st.graph_store().query(
        f"""SELECT ?label ?bu ?concept ?version WHERE {{
              <{iri}> a <{V}Entity> ; <{RDFS_LABEL}> ?label ; <{V}bu> ?bu .
              OPTIONAL {{ <{iri}> <{V}concept> ?concept }}
              OPTIONAL {{ <{iri}> <{V}fromVersion> ?version }}
            }}""",
        graphs,
    ) if graphs and ENTITY_IRI.fullmatch(iri) else []  # fmt: skip
    if not rows or rows[0]["bu"] not in set(bus):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown entity")
    flt = build_access_filter(ident)
    sources: dict[str, dict[str, Any]] = {}
    for vid in sorted({r["version"].removeprefix("urn:kb:version:") for r in rows if r.get("version")}):
        hit = st.chunk_index.version_chunks(vid, flt, size=1)  # the index ACL decides, as in the inspector
        if hit:
            c = hit[0].chunk
            sources[c.doc_id] = {"doc_id": c.doc_id, "title": c.metadata.get("title"), "version_id": vid,
                                 "version_no": c.metadata.get("version_no")}  # fmt: skip
    concept = rows[0]["concept"]
    hints: list[str] = []
    if concept:
        qa = st.qa()
        t2s = getattr(qa, "t2s", None)
        if t2s is not None and getattr(t2s, "table_map", None) is not None:
            hints = t2s.table_map.hints([concept], sorted(t2s.engine.permitted_schema(ident)))  # port API
    return {
        "iri": iri,
        "label": rows[0]["label"],
        "bu": rows[0]["bu"],
        "concept": concept,
        "sources": list(sources.values()),
        "mart_hints": hints,
    }
