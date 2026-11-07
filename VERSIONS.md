# Pinned versions

Policy: exact pins only. Python resolution is frozen with `exclude-newer = 2026-10-24` in
`pyproject.toml` (nothing newer than two weeks before Phase 0), and `uv.lock` pins all 182
packages including transitive ones. Change a pin only with a `DECISIONS.md` entry.

Last verified: 2026-11-07 (Phase 1).

## Runtime and tooling

| Component | Version | Source / verification |
|---|---|---|
| Python | 3.11.9 | `requires-python = "==3.11.*"` |
| uv | 0.13.6 | `uv --version` |
| Node.js / npm | 22.14.0 / 10.9.2 | installed on the host; used from Phase 6 (UI) |

## Infrastructure (official binaries, no Docker), pinned in `infra/local/kbinfra.py`

| Component | Version | Artifact | Integrity check |
|---|---|---|---|
| PostgreSQL | 17.10 (EDB build 17.10-1) | `postgresql-17.10-1-windows-x64-binaries.zip` | sha256 `f9aafca58e7026a1ef2caeee711acf761671e57904d430adc85f468374f5a821` (recorded at first download; EDB publishes no checksum) |
| Elasticsearch | 8.19.23 (bundled JDK: OpenJDK 27) | `elasticsearch-8.19.23-windows-x86_64.zip` | sha512 from artifacts.elastic.co, checked on fetch |
| Ollama | 0.40.8 | `ollama-windows-amd64.zip` (GitHub release) | sha256 from the release's `sha256sum.txt`, checked on fetch |
| Arize Phoenix | 20.26.0 | PyPI `arize-phoenix` (dependency group `phoenix`) | `uv.lock` |

## Models

| Port | Model | Pin | Status |
|---|---|---|---|
| LLM (local) | `qwen3:8b` (Q4_K_M, 5.23 GB) | Ollama digest `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41` | pulled, verified by integration test |
| Embedder (local) | `BAAI/bge-small-en-v1.5` (384-d) | HF revision to be pinned in Phase 2 | not downloaded yet |
| Reranker (local) | `BAAI/bge-reranker-base` | HF revision to be pinned in Phase 3 | not downloaded yet |
| Embedder (CML) | `BAAI/bge-m3` (1024-d) | pinned at CML deployment | n/a locally |
| Reranker (CML) | `BAAI/bge-reranker-v2-m3` | pinned at CML deployment | n/a locally |

## Python packages: direct dependencies

| Package | Version | Group |
|---|---|---|
| pydantic | 2.14.1 | main |
| pydantic-settings | 2.16.1 | main |
| pyyaml | 6.0.3 | main |
| httpx | 0.28.1 | main |
| psycopg[binary] | 3.3.6 | main |
| fsspec | 2026.9.0 | main |
| duckdb | 2.0.0 | main |
| fastapi | 0.162.0 | main |
| uvicorn | 0.54.0 | main |
| psycopg-pool | 3.3.3 | main |
| pytest | 9.1.1 | dev |
| ruff | 0.17.2 | dev |
| mypy | 2.4.0 | dev |
| types-PyYAML | 6.0.12.20260906 | dev |
| pypdf | 6.21.2 | dev |
| python-docx | 1.2.0 | synthetic |
| python-pptx | 1.0.2 | synthetic |
| openpyxl | 3.1.5 | synthetic |
| reportlab | 5.1.0 | synthetic |
| pillow | 13.0.0 | synthetic |
| arize-phoenix | 20.26.0 | phoenix |

Transitive packages: see `uv.lock` (182 packages).

## Planned, not yet installed (pinned when their phase starts)

Dagster, Docling (+ HybridChunker, OCR weights), Presidio, Haystack, elasticsearch-haystack,
sentence-transformers / torch (CUDA build), sqlglot, graphifyy, rdflib,
pyoxigraph, pySHACL, RAGAS, FIBO release, React, pdf.js.
Known at Phase 0: `haystack-ai` on PyPI is 3.x; see DECISIONS D-006.
