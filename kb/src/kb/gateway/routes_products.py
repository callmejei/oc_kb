"""Data products (Phase 9b, D-066), mounted at /api/v1/products. For makers and checkers of the `data` domain only.

The database enforces every guarantee (four-eyes, locking, single-actor restrictions, stale-approval refusal); this
layer validates specs against the real mart catalogue first (precise errors, sanitised row filters) and shows a
checker the change as a diff, marking what WIDENS access. Governing a product never grants reading it: a data maker
or checker sees policies, not mart rows.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from kb.gateway.deps import GatewayState, current_identity, db_errors, roles, state
from kb.ports.types import Identity
from kb.registry import products as repo
from kb.sql.policy_store import SpecError

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]
DOMAIN = "data"


def governor(ident: Ident, st: St) -> Identity:
    if DOMAIN not in set(roles(st, ident, "MAKER")) | set(roles(st, ident, "CHECKER")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "data products are governed by makers and checkers of 'data'")
    return ident


Gov = Annotated[Identity, Depends(governor)]


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grants: list[str] = Field(max_length=50)
    row_filters: list[dict[str, Any]] = Field(max_length=50)
    column_masks: dict[str, dict[str, Any]] = Field(max_length=100)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    owner: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
    refresh: str = Field(min_length=1, max_length=200)
    spec: Spec


class Comment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=2000)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    reason: str | None = Field(default=None, max_length=2000)


class Restriction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["REVOKE_GRANT", "ADD_MASK", "SUSPEND"]
    reason: str = Field(min_length=3, max_length=2000)
    group: str | None = Field(default=None, max_length=64)
    column: str | None = Field(default=None, max_length=63)
    mask: Literal["redact", "hash", "year_only", "null"] = "redact"


def _catalog(st: GatewayState) -> dict[str, tuple[str, ...]]:
    t2s = getattr(st.qa(), "t2s", None)
    if t2s is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "structured data is not configured")
    return {t: tp.columns for t, tp in t2s.engine._policy.catalog.items()}  # catalogue only: no policy needed


def diff_specs(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    """What changes, per item, and whether it WIDENS access (informational: the database requires a checker for
    every revision anyway)."""
    old = old or {"grants": [], "row_filters": [], "column_masks": {}}
    out: list[dict[str, Any]] = []
    for g in sorted(set(new["grants"]) - set(old["grants"])):
        out.append({"kind": "grant", "item": g, "change": "added", "widens": True})
    for g in sorted(set(old["grants"]) - set(new["grants"])):
        out.append({"kind": "grant", "item": g, "change": "removed", "widens": False})

    def filters(s: dict[str, Any]) -> dict[str, set[str]]:
        by: dict[str, set[str]] = {}
        for f in s["row_filters"]:
            for g in f["groups"]:
                by.setdefault(g, set()).add(f["filter"])
        return by

    fo, fn = filters(old), filters(new)
    for g in sorted(set(fo) | set(fn)):
        a, b = fo.get(g, set()), fn.get(g, set())
        if a != b:
            out.append({"kind": "row_filter", "item": g, "change": f"{sorted(a) or ['(no rows)']} -> "
                        f"{sorted(b) or ['(no rows)']}", "widens": bool(b - a)})  # fmt: skip
    mo, mn = old["column_masks"], new["column_masks"]
    for c in sorted(set(mo) | set(mn)):
        a, b = mo.get(c), mn.get(c)
        if a == b:
            continue
        if b is None:
            out.append({"kind": "mask", "item": c, "change": "mask removed", "widens": True})
        elif a is None:
            out.append(
                {"kind": "mask", "item": c, "change": f"masked ({b['mask']})", "widens": bool(b["unless_groups"])}
            )
        else:
            wider = bool(set(b["unless_groups"]) - set(a["unless_groups"])) or a["mask"] != b["mask"]
            out.append({"kind": "mask", "item": c, "change": f"{a} -> {b}", "widens": wider})
    return out


def _rows(st: GatewayState, ident: Identity, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with st.registry.transaction(ident.user_id) as tx:
        return list(tx.execute(sql, params).fetchall())


# ------------------------------------------------------------------------------------------------ read
@router.get("")
def list_products(ident: Gov, st: St) -> dict[str, Any]:
    cat = _catalog(st)
    rows = _rows(
        st,
        ident,
        """SELECT p.product_id, p.status, p.gov_bu, r.revision_no, r.owner, r.description, r.classification,
                  r.refresh, r.spec,
                  (SELECT count(*) FROM kb.product_request q WHERE q.product_id = p.product_id
                     AND q.status = 'PENDING') AS pending
             FROM kb.data_product p LEFT JOIN kb.product_revision r ON r.revision_id = p.current_revision_id
            ORDER BY p.product_id""",
    )
    known = {r["product_id"] for r in rows}
    return {
        "products": rows,
        "unregistered": sorted(set(cat) - known),  # mart tables with no product: denied to everyone
        "policy_version": int(_rows(st, ident, "SELECT kb.policy_version() AS v")[0]["v"]),
        "maker": DOMAIN in roles(st, ident, "MAKER"),
        "checker": DOMAIN in roles(st, ident, "CHECKER"),
        "columns": {t: list(c) for t, c in cat.items()},
    }


@router.get("/requests")
def requests(ident: Gov, st: St, status_filter: Literal["PENDING", "ALL"] = "PENDING") -> list[dict[str, Any]]:
    where = "WHERE q.status = 'PENDING'" if status_filter == "PENDING" else ""
    rows = _rows(
        st,
        ident,
        f"""SELECT q.request_id, q.request_type, q.product_id, q.maker_id, q.maker_comment, q.submitted_at,
                   q.status, q.checker_id, q.decision_reason, q.decided_at, r.revision_no, r.spec AS proposed,
                   r.owner, r.description, r.classification, r.refresh, r.change_kind,
                   cur.spec AS current, cur.revision_no AS current_revision_no,
                   (r.base_revision_id IS DISTINCT FROM p.current_revision_id
                    AND q.request_type IN ('ONBOARD_PRODUCT','CHANGE_PRODUCT') AND q.status = 'PENDING') AS stale
              FROM kb.product_request q JOIN kb.data_product p ON p.product_id = q.product_id
              LEFT JOIN kb.product_revision r ON r.revision_id = q.revision_id
              LEFT JOIN kb.product_revision cur ON cur.revision_id = p.current_revision_id
              {where} ORDER BY q.submitted_at DESC LIMIT 200""",  # noqa: S608 - fixed SQL; one of two literal clauses
    )
    for r in rows:
        if r["request_type"] in ("ONBOARD_PRODUCT", "CHANGE_PRODUCT") and r["proposed"] is not None:
            base = None if r["request_type"] == "ONBOARD_PRODUCT" else r["current"]
            r["diff"] = diff_specs(base, r["proposed"])
            r["widens"] = any(d["widens"] for d in r["diff"])
        elif r["request_type"] == "REACTIVATE_PRODUCT":
            r["diff"], r["widens"] = [{"kind": "status", "item": r["product_id"], "change": "SUSPENDED -> ACTIVE",
                                       "widens": True}], True  # fmt: skip
        r["own"] = r["maker_id"] == ident.user_id
    return rows


@router.get("/drafts")
def my_drafts(ident: Gov, st: St) -> list[dict[str, Any]]:
    return _rows(
        st,
        ident,
        """SELECT revision_id, product_id, revision_no, owner, description, classification, refresh, spec, created_at
             FROM kb.product_revision WHERE status = 'DRAFT' AND created_by = %s ORDER BY created_at DESC""",
        (ident.user_id,),
    )


# ------------------------------------------------------------------------------------------------ maker
@router.post("/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(body: Draft, ident: Gov, st: St) -> dict[str, Any]:
    cat = _catalog(st)
    if body.product_id not in cat:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{body.product_id} is not a mart table")
    try:
        with st.registry.transaction(ident.user_id) as tx, db_errors():
            rev = repo.draft(
                tx,
                body.product_id,
                cat[body.product_id],
                owner=body.owner,
                description=body.description,
                classification=body.classification,
                refresh=body.refresh,
                spec=body.spec.model_dump(),
            )
    except SpecError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    return {"revision_id": rev}


@router.delete("/drafts/{revision_id}")
def discard_draft(revision_id: str, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        tx.execute("SELECT kb.discard_product_draft(%s)", (revision_id,))
    return {"discarded": revision_id}


@router.post("/drafts/{revision_id}/submit", status_code=status.HTTP_201_CREATED)
def submit(revision_id: str, body: Comment, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        return {"request_id": repo.submit(tx, revision_id, body.comment)}


@router.post("/requests/{request_id}/withdraw")
def withdraw(request_id: str, body: Comment, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        repo.withdraw(tx, request_id, body.comment)
    return {"withdrawn": request_id}


@router.post("/{product_id}/restrict", status_code=status.HTTP_201_CREATED)
def restrict(product_id: str, body: Restriction, ident: Gov, st: St) -> dict[str, Any]:
    """Single actor: narrows access at once (D-066 P5). The database derives the new policy."""
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        req = repo.restrict(
            tx, product_id, body.action, body.reason, group=body.group, column=body.column, mask=body.mask
        )
    return {"request_id": req, "status": "EXECUTED"}


@router.post("/{product_id}/reactivate", status_code=status.HTTP_201_CREATED)
def reactivate(product_id: str, body: Comment, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        return {"request_id": repo.request_reactivation(tx, product_id, body.comment or "")}


# ------------------------------------------------------------------------------------------------ checker
@router.post("/requests/{request_id}/decide")
def decide(request_id: str, body: Decision, ident: Gov, st: St) -> dict[str, Any]:
    with st.registry.transaction(ident.user_id) as tx, db_errors():
        repo.decide(tx, request_id, body.approve, body.reason)
    return {"request_id": request_id, "status": "APPROVED" if body.approve else "REJECTED"}
