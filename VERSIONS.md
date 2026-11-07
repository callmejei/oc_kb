# Pinned versions

Policy: exact pins only. Python resolution is frozen with `exclude-newer = 2026-10-24` in
`pyproject.toml` (nothing newer than two weeks before Phase 0), and `uv.lock` pins all 182
packages including transitive ones. Change a pin only with a `DECISIONS.md` entry.

Last verified: 2026-11-07 (Phase 4).

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
| Embedder (local) | `BAAI/bge-small-en-v1.5` (384-d) | HF commit `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` (checked at load) | `kb.ps1 models`, offline at runtime |
| Parsing (local) | Docling layout (heron), TableFormer, RapidOCR PP-OCRv6 small (torch) | versions pinned by docling 2.142.1's downloader | `kb.ps1 models` -> `.infra/models/docling` |
| PII NER | spaCy `en_core_web_lg` 3.8.0 | pip wheel, URL in `uv.lock` | installed with the env |
| Reranker (local) | `BAAI/bge-reranker-base` | HF commit `2cfc18c9415c912f9d8155881c133215df768a70` (checked at load) | `kb.ps1 models`, offline at runtime |
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
| python-multipart | 0.0.32 | main |
| haystack-ai | 2.31.0 (pulls openai 3.37.0, never imported by our code) | main |
| pyjwt | 2.15.1 | main |
| sqlglot | 30.24.0 | main |
| dagster, dagster-webserver | 1.13.26 | main |
| docling | 2.142.1 (docling-core 2.104.0, docling-ibm-models 4.0.4, rapidocr 3.10.0) | main |
| presidio-analyzer, presidio-anonymizer | 2.2.364 | main |
| spacy | 3.8.16 | main |
| en-core-web-lg | 3.8.0 (wheel URL pinned in uv.lock) | main |
| sentence-transformers | 6.2.0 (transformers 5.20.2, torch 2.14.1 CPU) | main |
| elasticsearch (client) | 8.19.3 (9.x is incompatible with the 8.19 server) | main |
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
| arize-phoenix | 20.26.0 | isolated env `infra/local/phoenix` (D-019) |

Transitive packages: see `uv.lock` (182 packages).

## Planned, not yet installed (pinned when their phase starts)

graphifyy, rdflib,
pyoxigraph, pySHACL, RAGAS, FIBO release, React, pdf.js.
elasticsearch-haystack: not used (D-028).
