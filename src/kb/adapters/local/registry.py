"""PostgresRegistry: connection pool; every transaction carries the acting user in kb.actor_id."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class PostgresRegistry:
    STATUS = "implemented"
    PHASE = 1

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 8, **_: Any) -> None:
        self.dsn = dsn
        self._pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] | None = None
        self._lock = threading.Lock()
        self._sizes = (min_size, max_size)

    def _get_pool(self) -> ConnectionPool[psycopg.Connection[dict[str, Any]]]:
        with self._lock:  # lazy: building the adapter never touches the network
            if self._pool is None:
                self._pool = ConnectionPool(
                    self.dsn,
                    min_size=self._sizes[0],
                    max_size=self._sizes[1],
                    kwargs={"autocommit": True, "row_factory": dict_row},
                    open=True,
                    name="kb-registry",
                )
            return self._pool

    @contextmanager
    def transaction(self, actor_id: str) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """One DB transaction acting as ``actor_id`` (SET LOCAL semantics: cleared at commit)."""
        if not actor_id:
            raise ValueError("actor_id is required")
        with self._get_pool().connection() as conn, conn.transaction():
            conn.execute("SELECT set_config('kb.actor_id', %s, true)", (actor_id,))
            yield conn

    def ping(self) -> bool:
        try:
            with self._get_pool().connection() as conn:
                return conn.execute("SELECT 1 AS ok").fetchone() == {"ok": 1}
        except psycopg.Error:
            return False

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
