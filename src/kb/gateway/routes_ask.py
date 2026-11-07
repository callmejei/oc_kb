"""Document Q&A and local login."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from kb.adapters.local.identity import UnknownUserError
from kb.gateway.deps import GatewayState, current_identity, db_errors, state
from kb.ports.types import Identity

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=2000)
    spaces: list[str] | None = Field(default=None, max_length=10)  # BU spaces: narrow only


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_id: str
    rating: Literal["UP", "DOWN"]
    problem: str | None = Field(default=None, max_length=40)
    reason: str | None = Field(default=None, max_length=2000)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.post("/ask")
def ask(body: AskBody, ident: Ident, st: St) -> Any:
    return st.qa().ask(ident, body.question, body.spaces).as_response()


@router.get("/me")
def me(ident: Ident, st: St) -> Any:
    """Who the UI is talking to: readable BU spaces and governance roles (for showing, never deciding)."""
    gov = st.idp.governance_roles(ident)
    return {
        "user_id": ident.user_id,
        "display_name": ident.display_name,
        "bu": ident.bu,
        "spaces": sorted(st.idp.readable_bus(ident)),
        "maker_bus": sorted(bu for bu, r in gov if r == "MAKER"),
        "checker_bus": sorted(bu for bu, r in gov if r == "CHECKER"),
    }


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
def feedback(body: FeedbackBody, ident: Ident, st: St) -> Any:
    """Thumbs up/down on one of the caller's own answers. The database copies the cited versions from the
    caller's QUERY audit row; nothing about the answer is taken from the client."""
    with st.registry.transaction(ident.user_id) as conn, db_errors():
        row = conn.execute(
            "SELECT kb.submit_feedback(%s, %s, %s, %s) AS id", (body.query_id, body.rating, body.problem, body.reason)
        ).fetchone()
    assert row is not None
    return {"feedback_id": row["id"]}


@router.post("/auth/login")
def login(body: LoginBody, st: St) -> Any:
    if st.issuer is None or st.passwords is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "local login is not enabled")
    try:
        st.idp.resolve(body.username)
        known = True
    except UnknownUserError:
        known = False
    ok = st.passwords.check(body.username if known else "\x00unknown", body.password) and known
    if not ok:  # unknown user and wrong password are indistinguishable
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    return st.issuer.issue(body.username)
