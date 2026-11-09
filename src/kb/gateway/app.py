"""Gateway FastAPI app (the one door). Phase 1: governance endpoints only.

Run locally:  uv run uvicorn kb.gateway.app:create_app --factory --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from kb.adapters import adapter_class, build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.adapters.local.registry import PostgresRegistry
from kb.config import REPO_ROOT, Settings, load_settings
from kb.gateway.auth import LocalPasswordStore, TokenIssuer
from kb.gateway.deps import GatewayState
from kb.gateway.routes_ask import router as ask_router
from kb.gateway.routes_documents import router as documents_router
from kb.gateway.routes_explorer import router as explorer_router
from kb.gateway.routes_governance import router as governance_router
from kb.gateway.routes_ontology import router as ontology_router
from kb.gateway.routes_ops import router as ops_router
from kb.gateway.routes_products import router as products_router
from kb.gateway.routes_quality import router as quality_router
from kb.gateway.routes_semantic import router as semantic_router
from kb.gateway.routes_uploads import router as uploads_router


def create_app(
    settings: Settings | None = None,
    registry: PostgresRegistry | None = None,
    idp: YamlIdentityProvider | None = None,
    trusted_clients: frozenset[str] | None = None,
    raw_store: Any = None,
    chunk_index: Any = None,
    qa_factory: Any = None,
    processed_store: Any = None,
    ui_dist: Path | None = None,
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
        processed_store=processed_store or processed_port(settings),
    )
    secret = str(gw.get("jwt_secret", ""))
    if gw.get("auth") == "local_jwt" and secret and "${" not in secret:
        st.issuer = TokenIssuer(secret, int(gw.get("token_ttl_minutes", 60)))
        users = Path(str(gw.get("local_users", ".infra/secrets/local_users.hash.json")))
        st.passwords = LocalPasswordStore(users if users.is_absolute() else REPO_ROOT / users)
    st.qa_factory = qa_factory or (lambda: default_qa(settings, st))
    st.graph_factory = lambda: build(settings, "graph_store")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        st.registry.close()

    app = FastAPI(title="KB gateway", version="0.1.0", lifespan=lifespan)
    app.state.kb = st
    app.include_router(governance_router, prefix="/api/v1/governance", tags=["governance"])
    app.include_router(uploads_router, prefix="/api/v1/uploads", tags=["uploads"])
    app.include_router(ask_router, prefix="/api/v1", tags=["ask"])
    app.include_router(documents_router, prefix="/api/v1/documents", tags=["documents"])
    app.include_router(ops_router, prefix="/api/v1/ops", tags=["ops"])
    app.include_router(ontology_router, prefix="/api/v1/ontology", tags=["ontology"])
    app.include_router(explorer_router, prefix="/api/v1/explorer", tags=["explorer"])
    app.include_router(quality_router, prefix="/api/v1/quality", tags=["quality"])
    app.include_router(products_router, prefix="/api/v1/products", tags=["products"])
    app.include_router(semantic_router, prefix="/api/v1/semantic", tags=["semantic"])

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"registry": st.registry.ping()}

    mount_ui(app, ui_dist or REPO_ROOT / "ui" / "dist")
    return app


CSP = (
    "default-src 'self'; script-src 'self'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def mount_ui(app: FastAPI, dist: Path) -> None:
    """Serve the built React app on the gateway's own port (one door; a CML Application exposes one port).
    Unknown non-API paths return index.html (client-side routes). Every response gets a strict CSP."""
    from fastapi import Request
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    index = dist / "index.html"
    if not index.exists():
        return  # API-only (tests, or the UI not built): `kb.ps1 ui` builds it
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Any:
        if path.startswith("api/"):
            from fastapi import HTTPException

            raise HTTPException(404, "not found")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def processed_port(settings: Settings) -> Any:
    """Parse outputs (Docling JSON) for the inspector: the raw_store adapter on the processed URL."""
    pc = settings.ports["raw_store"]
    return adapter_class(pc.adapter)(**(pc.options | {"url": settings.processing["processed_url"]}))


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
    from kb.semantic.store import MetricSource

    return AskService(
        retriever,
        llm,
        st.registry,
        settings.retrieval,
        t2s,
        scope_fn=scope,
        tracer=build(settings, "tracer"),
        metrics=MetricSource.from_settings(settings, t2s.engine),
    )
