"""OxigraphGraphStore: named graphs in pyoxigraph, persisted as one N-Quads file replaced atomically.

Writers (the ontology build) and readers (the gateway) are separate processes; an on-disk RocksDB store
can only be opened by one process, so the persisted form is an N-Quads file written to a temp name and
renamed. Readers keep an in-memory store and reload it when the file changes.

Isolation (graphify-fibo skill): `query` takes the list of graph IRIs the caller may read and runs the
SPARQL with exactly those as BOTH the default graph and the named-graph set; other graphs are invisible,
including to `GRAPH ?g { ... }` patterns.
"""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pyoxigraph as ox

from kb.config import REPO_ROOT

DUMP = "kb.nq"


class OxigraphGraphStore:
    STATUS = "implemented"
    PHASE = 5

    def __init__(self, path: str, **_: Any) -> None:
        p = Path(path)
        self.dir = p if p.is_absolute() else REPO_ROOT / p
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / DUMP
        self._store = ox.Store()
        self._mtime: float | None = None
        self._lock = threading.RLock()
        self._reload(force=True)

    def _reload(self, force: bool = False) -> None:
        with self._lock:
            mtime = self.file.stat().st_mtime if self.file.exists() else None
            if not force and mtime == self._mtime:
                return
            store = ox.Store()
            if mtime is not None:
                store.load(path=str(self.file), format=ox.RdfFormat.N_QUADS)
            self._store, self._mtime = store, mtime

    def _persist(self) -> None:
        buf = io.BytesIO()
        self._store.dump(buf, format=ox.RdfFormat.N_QUADS)
        tmp = self.file.with_suffix(".nq.tmp")
        tmp.write_bytes(buf.getvalue())
        os.replace(tmp, self.file)
        self._mtime = self.file.stat().st_mtime

    # ---------------------------------------------------------------- port
    def replace_graph(self, graph_iri: str, ntriples: Iterator[str]) -> None:
        """Load into <graph>:staging, then one SPARQL MOVE makes it live (atomic in the store)."""
        with self._lock:
            self._reload()
            staging = f"{graph_iri}:staging"
            self._store.update(f"DROP SILENT GRAPH <{staging}>")
            data = "".join(line if line.endswith("\n") else line + "\n" for line in ntriples).encode("utf-8")
            if data:
                self._store.load(data, format=ox.RdfFormat.N_TRIPLES, to_graph=ox.NamedNode(staging))
            self._store.update(f"MOVE SILENT GRAPH <{staging}> TO <{graph_iri}>")
            self._persist()

    def query(self, sparql: str, graph_iris: Sequence[str]) -> list[dict[str, Any]]:
        """SELECT over exactly `graph_iris` (as the default graph). An empty list sees nothing."""
        self._reload()
        if not graph_iris:
            return []
        with self._lock:
            default: list[ox.NamedNode | ox.BlankNode | ox.DefaultGraph] = [ox.NamedNode(g) for g in graph_iris]
            named: list[ox.NamedNode | ox.BlankNode] = [ox.NamedNode(g) for g in graph_iris]
            # BOTH datasets must be restricted: named_graphs defaults to *all* graphs, so without it a
            # `GRAPH ?g { ... }` pattern would read other BUs' graphs (found by test, Phase 5).
            res = self._store.query(sparql, default_graph=default, named_graphs=named, use_default_graph_as_union=False)
            out = []
            for sol in res:  # type: ignore[union-attr]
                row = {}
                for v in res.variables:  # type: ignore[union-attr]
                    term = sol[v]
                    row[v.value] = None if term is None else term.value
                out.append(row)
            return out

    def graphs(self) -> list[str]:
        self._reload()
        with self._lock:
            return sorted(g.value for g in self._store.named_graphs())

    def count(self, graph_iri: str) -> int:
        self._reload()
        with self._lock:
            return sum(1 for _ in self._store.quads_for_pattern(None, None, None, ox.NamedNode(graph_iri)))
