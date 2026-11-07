"""Gateway FastAPI app (the one door). Phase 1: governance endpoints only.

Run locally:  uv run uvicorn kb.gateway.app:create_app --factory --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from kb.adapters import build
from kb.adapters.local.identity import YamlIdentityProvider
from kb.adapters.local.registry import PostgresRegistry
from kb.config import Settings, load_settings
from kb.gateway.deps import GatewayState
from kb.gateway.routes_governance import router as governance_router
from kb.gateway.routes_uploads import router as uploads_router


def create_app(
    settings: Settings | None = None,
    registry: PostgresRegistry | None = None,
    idp: YamlIdentityProvider | None = None,
    trusted_clients: frozenset[str] | None = None,
    raw_store: Any = None,
    chunk_index: Any = None,
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        st.registry.close()

    app = FastAPI(title="KB gateway", version="0.1.0", lifespan=lifespan)
    app.state.kb = st
    app.include_router(governance_router, prefix="/api/v1/governance", tags=["governance"])
    app.include_router(uploads_router, prefix="/api/v1/uploads", tags=["uploads"])

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"registry": st.registry.ping()}

    return app
