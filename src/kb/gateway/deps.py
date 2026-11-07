"""Gateway dependencies: identity resolution and DB error mapping."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import HTTPException, Request, status
from psycopg import errors as pg

from kb.adapters.local.identity import UnknownUserError, YamlIdentityProvider
from kb.adapters.local.registry import PostgresRegistry
from kb.ports.types import Identity
from kb.registry.repository import ConflictError, NotFoundError

DEV_HEADER = "X-KB-User"


@dataclass(frozen=True)
class GatewayState:
    registry: PostgresRegistry
    idp: YamlIdentityProvider
    auth_mode: str
    trusted_clients: frozenset[str]


def state(request: Request) -> GatewayState:
    st: GatewayState = request.app.state.kb
    return st


def current_identity(request: Request) -> Identity:
    """Resolve the caller. Phase 1 dev mode: X-KB-User from a trusted loopback client only."""
    st = state(request)
    if st.auth_mode != "dev_header":
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "only dev_header auth exists before Phase 3")
    client = request.client.host if request.client else ""
    if client not in st.trusted_clients:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "dev auth is accepted only from loopback clients")
    user_id = request.headers.get(DEV_HEADER, "").strip()
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"missing {DEV_HEADER} header")
    try:
        return st.idp.resolve(user_id)
    except UnknownUserError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user") from None


def roles(st: GatewayState, ident: Identity, role: str) -> list[str]:
    """BUs where the identity holds a governance role (MAKER / CHECKER)."""
    return sorted(bu for bu, r in st.idp.governance_roles(ident) if r == role)


@contextmanager
def db_errors() -> Iterator[None]:
    """Map database refusals to HTTP. The DB is the authority; the message is passed through."""
    try:
        yield
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    except ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except pg.InsufficientPrivilege as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _msg(e)) from None
    except pg.NoDataFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _msg(e)) from None
    except (pg.UniqueViolation, pg.RaiseException) as e:
        raise HTTPException(status.HTTP_409_CONFLICT, _msg(e)) from None
    except (pg.CheckViolation, pg.ForeignKeyViolation, pg.NotNullViolation, pg.InvalidTextRepresentation) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _msg(e)) from None


def _msg(e: psycopg.Error) -> str:
    diag: Any = e.diag
    return str(diag.message_primary or e)
