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
| RAGAS judge (local) | `llama3.1:8b` (Q4_K_M, 4.92 GB) | Ollama digest `46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e` | pulled 2026-11-08; `evaluation.judge_model` (D-057) |
| graphify extractor (local) | `qwen3-graphify` = `qwen3:8b` weights + `infra/local/ollama/qwen3-graphify.Modelfile` (thinking off, top_p 0.8, top_k 20, presence_penalty 1.5, seed 42) | same blobs as `qwen3:8b`; Modelfile in git | `kb.ps1 models` creates it (D-040) |
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
| rdflib | 7.6.0 | main |
| pyoxigraph | 0.5.13 | main |
| pyshacl | 0.40.1 | main |
| psutil | 7.2.2 | dev |
| graphifyy[ollama,office] | 0.9.95 | isolated env `infra/local/graphify`, uv-locked, exclude-newer 2026-10-24 (D-039) |
| ragas | 0.4.3 | isolated env `infra/local/ragas`, uv-locked, exclude-newer 2026-10-25; with langchain-community 0.3.31 (<0.4, D-057), sentence-transformers 6.2.0, torch 2.14.1+cpu, transformers 5.20.2 |
| opentelemetry-sdk | 1.45.1 | main (Phase 7 tracer; matches opentelemetry-api 1.45.1) |
| opentelemetry-exporter-otlp-proto-http | 1.45.1 | main (OTLP/HTTP to Phoenix `/v1/traces`) |

## Ontology (Phase 5)

| Item | Pin |
|---|---|
| FIBO | release `master_2026Q3`, archive sha256 `46385076935d28e65c3e3a4b68b78416948e5ca1afbd0dfe312c3fe8a0010694`, in `.infra/ontology/fibo` (D-038); modules FND, BE, FBC, LOAN, SEC (1,881 classes) |
| Mapping thresholds (`config/ontology.yaml`) | auto_accept 0.85 (rule matches only, D-042); exact label 1.0, synonym 0.95, head term 0.88, partial term 0.75, embed_min 0.55 |
| graphify run settings (`config/local.yaml` `graph:`) | token_budget 400, part_chars 2000, num_ctx 12288, max_output_tokens 3072, api_timeout_s 300, timeout_s 14400, temperature 0.7, seed 42 (D-040) |

Transitive packages: see `uv.lock` (182 packages).

## UI (Phase 6), npm, exact pins in `ui/package.json` + `ui/package-lock.json`

| Package | Version | Note |
|---|---|---|
| react / react-dom | 18.3.1 | |
| react-router-dom | 7.18.4 | 6.x has an open-redirect advisory fixed only in >= 7.18.0 (D-050) |
| pdfjs-dist | 4.10.38 | rendered with `isEvalSupported: false`, `enableXfa: false` |
| three | 0.171.0 | ontology star map (WebGL); no CDN, bundled by vite |
| @types/three | 0.171.0 | dev |
| @fontsource/nunito-sans | 5.3.0 | explorer font, bundled (strict CSP: no Google Fonts); SIL OFL 1.1 (D-061) |
| vite | 8.3.5 | vite <= 6.4.3 has dev-server path-traversal advisories (D-050) |
| @vitejs/plugin-react | 6.1.2 | |
| typescript | 5.6.3 | |
| @types/react / @types/react-dom / @types/node | 18.3.12 / 18.3.1 / 22.20.5 | |
| @playwright/test | 1.64.1 | < 1.55.1 downloads browsers without TLS verification (D-050) |
| Chromium (Playwright headless shell) | 156.0.8078.4 (build v1248) | `npx playwright install chromium`; test-only |

`npm audit`: 0 vulnerabilities at pin time (2026-11-08). Every pin is at least two weeks old.

## Planned, not yet installed (pinned when their phase starts)

Nothing pending. elasticsearch-haystack: not used (D-028).
