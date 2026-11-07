"""Gateway FastAPI app (the one door). Phase 1: governance endpoints only.

Run locally:  uv run uvicorn kb.gateway.app:create_app --factory --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.adapters.local.registry import PostgresRegistry
from kb.config import REPO_ROOT, Settings, load_settings
from kb.gateway.auth import LocalPasswordStore, TokenIssuer
from kb.gateway.deps import GatewayState
from kb.gateway.routes_ask import router as ask_router
from kb.gateway.routes_governance import router as governance_router
from kb.gateway.routes_uploads import router as uploads_router


def create_app(
    settings: Settings | None = None,
    registry: PostgresRegistry | None = None,
    idp: YamlIdentityProvider | None = None,
    trusted_clients: frozenset[str] | None = None,
    raw_store: Any = None,
    chunk_index: Any = None,
    qa_factory: Any = None,
) -> FastAPI:
    settings = settings or load_settings(strict=True)
    gw = settings.gateway
    st = GatewayState(
        registry=registry or build(settings, "registry"),
        idp=idp or build(settings, "identity_provider"),
        auth_mode=str(gw.get("auth", "")),
        trusted_clients=trusted_clients or frozenset(gw.get("trusted_clients", [])),
        raw_store=raw_store or build(settings, "raw_store"),
        chunk_index=chunk_index or build(settings, "chunk_index"),
    )
    secret = str(gw.get("jwt_secret", ""))
    if gw.get("auth") == "local_jwt" and secret and "${" not in secret:
        st.issuer = TokenIssuer(secret, int(gw.get("token_ttl_minutes", 60)))
        users = Path(str(gw.get("local_users", ".infra/secrets/local_users.hash.json")))
        st.passwords = LocalPasswordStore(users if users.is_absolute() else REPO_ROOT / users)
    st.qa_factory = qa_factory or (lambda: default_qa(settings, st))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        st.registry.close()

    app = FastAPI(title="KB gateway", version="0.1.0", lifespan=lifespan)
    app.state.kb = st
    app.include_router(governance_router, prefix="/api/v1/governance", tags=["governance"])
    app.include_router(uploads_router, prefix="/api/v1/uploads", tags=["uploads"])
    app.include_router(ask_router, prefix="/api/v1", tags=["ask"])

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"registry": st.registry.ping()}

    return app


def default_qa(settings: Settings, st: GatewayState) -> Any:
    from kb.agent.service import AskService
    from kb.gateway.acl import graph_scope
    from kb.graph.ontology import Ontology
    from kb.graph.table_map import TableMap
    from kb.retrieval.expansion import GraphExpander
    from kb.retrieval.pipeline import HybridRetriever
    from kb.sql.text_to_sql import TextToSql

    llm = build(settings, "llm")
    ontology = Ontology()
    expander = None
    if settings.graph.get("expansion"):
        expander = GraphExpander(
            build(settings, "graph_store"), ontology, int(settings.graph.get("expansion_terms", 10))
        )
    retriever = HybridRetriever(
        st.chunk_index, build(settings, "embedder"), build(settings, "reranker"), settings.retrieval, expander
    )
    t2s = TextToSql(build(settings, "sql_engine"), llm, table_map=TableMap(ontology))
    scope = (lambda ident: graph_scope(ident, st.idp.readable_bus(ident))) if expander else None
    return AskService(retriever, llm, st.registry, settings.retrieval, t2s, scope_fn=scope)
