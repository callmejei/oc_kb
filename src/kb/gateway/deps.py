"""Gateway dependencies: identity resolution and DB error mapping."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import HTTPException, Request, status
from psycopg import errors as pg

from kb.adapters.local.identity import UnknownUserError, YamlIdentityProvider
from kb.adapters.local.registry import PostgresRegistry
from kb.gateway.auth import AuthError, LocalPasswordStore, TokenIssuer
from kb.ports import ChunkIndex, RawStore
from kb.ports.types import Identity
from kb.registry.repository import ConflictError, NotFoundError

DEV_HEADER = "X-KB-User"


@dataclass
class GatewayState:
    registry: PostgresRegistry
    idp: YamlIdentityProvider
    auth_mode: str
    trusted_clients: frozenset[str]
    raw_store: RawStore
    chunk_index: ChunkIndex
    processed_store: RawStore
    issuer: TokenIssuer | None = None
    passwords: LocalPasswordStore | None = None
    qa_factory: Callable[[], Any] | None = None
    graph_factory: Callable[[], Any] | None = None
    _qa: Any = None
    _graph: Any = None

    def graph_store(self) -> Any:
        """GraphStore, built on first use (read-only use: queries are scoped to the caller's BU graphs)."""
        if self._graph is None:
            if self.graph_factory is None:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "graph store is not configured")
            self._graph = self.graph_factory()
        return self._graph

    def qa(self) -> Any:
        """AskService, built on first use (loads the embedder, reranker and LLM client)."""
        if self._qa is None:
            if self.qa_factory is None:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Q&A is not configured")
            self._qa = self.qa_factory()
        return self._qa


def state(request: Request) -> GatewayState:
    st: GatewayState = request.app.state.kb
    return st


def current_identity(request: Request) -> Identity:
    """Resolve the caller: a valid Bearer token (local_jwt), or the X-KB-User dev header from an explicitly
    trusted test client. Groups are always resolved live from the IdentityProvider."""
    st = state(request)
    authz = request.headers.get("Authorization", "")
    dev_user = request.headers.get(DEV_HEADER, "").strip()
    if authz.lower().startswith("bearer "):
        if st.issuer is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token auth is not enabled")
        try:
            user_id = st.issuer.verify(authz[7:].strip())
        except AuthError as e:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from None
    elif dev_user:
        client = request.client.host if request.client else ""
        if client not in st.trusted_clients:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "the dev header is accepted only from trusted test clients")
        user_id = dev_user
    else:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
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
