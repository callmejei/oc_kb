"""Document Q&A and local login."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from kb.adapters.local.identity import UnknownUserError
from kb.gateway.deps import GatewayState, current_identity, state
from kb.ports.types import Identity

router = APIRouter()
Ident = Annotated[Identity, Depends(current_identity)]
St = Annotated[GatewayState, Depends(state)]


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=2000)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.post("/ask")
def ask(body: AskBody, ident: Ident, st: St) -> Any:
    return st.qa().ask(ident, body.question).as_response()


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
