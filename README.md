# Enterprise Knowledge Base: local prototype

In-house knowledge platform for the Group Data Office: cited answers over BU documents
(SharePoint + uploads) and Ranger-secured marts, with per-BU access control enforced before
retrieval. Brief: `CLAUDE.md`. Decisions: `DECISIONS.md`. Versions: `VERSIONS.md`.

## Quick start (Windows laptop)

```powershell
.\kb.ps1 up      # fetch + verify binaries (first run only); start PostgreSQL, Elasticsearch, Ollama, Phoenix, Dagster
.\kb.ps1 models  # first run only: LLM (~5 GB), embedder and Docling models at pinned revisions
.\kb.ps1 seed    # rebuild the synthetic world under data/
.\kb.ps1 migrate # apply registry migrations to db `kb` and sync governance roles from users.yaml
.\kb.ps1 onboard # onboard the synthetic sources (maker -> checker) and wait for the daemon's first loads
.\kb.ps1 ui      # build the React UI (npm ci + vite build) -> ui/dist, served by the gateway
.\kb.ps1 api     # gateway + UI on http://127.0.0.1:8000 (OpenAPI at /docs)
                 #   sign in as a synthetic user; passwords: .infra/secrets/local_users.txt
.\kb.ps1 e2e     # browser acceptance (Playwright, headless Chromium) against the running stack;
                 #   watch it: cd ui; npx playwright test --headed   (or --ui); report: docs/e2e-report/
.\kb.ps1 test    # ruff + mypy + pytest (unit, security, integration, retrieval-level leak suite)
.\kb.ps1 leak    # end-to-end leak matrix with real LLM answers (slow)
.\kb.ps1 golden  # golden-set evaluation -> docs/eval/golden-latest.json
                 #   structured suite: uv run python -m kb.evaluation.golden --suite structured --out <file>
.\kb.ps1 eval    # Phase 7: golden sets per BU + RAGAS (local judge llama3.1:8b) + leak suite -> PASS/FAIL,
                 #   docs/eval/report-<ts>.json/.html, recorded in kb.eval_run (shown on the Operations page)
.\kb.ps1 down
```

On Linux/CML use the same targets with `make`. Local endpoints (loopback only):

| Service | URL | Credentials |
|---|---|---|
| PostgreSQL 17.10 | 127.0.0.1:5433, db `kb` | `kb_admin` / `KB_PG_ADMIN_PASSWORD` in `.env.local` |
| Elasticsearch 8.19.23 | http://127.0.0.1:9250 | `elastic` / `KB_ES_PASSWORD` in `.env.local` |
| Ollama 0.40.8 | http://127.0.0.1:11435 | none (loopback) |
| Phoenix 20.26.0 | http://127.0.0.1:6006 | none (loopback) |
| Dagster 1.13.26 | http://127.0.0.1:3070 | none (loopback); sensors/schedules on by default |

`.env.local` is generated on the first `up` and is gitignored.

## Synthetic world

`python -m synthetic seed` writes, deterministically:
- `data/sharepoint/<bu>/KB/...`: 39 documents (12 DOCX, 7 PDF, 5 two-column PDF, 6 scanned image-only
  PDF, 4 PPTX, 5 XLSX) across retail, corporate, wealth and shared, with LTV tables and 8 canary
  facts; `_ledger.json` simulates SharePoint server state (item ids, cTag, deleted, change sequence).
- `data/upload_fixtures/`: the 7 upload scenarios from CLAUDE.md section 6.
- `data/marts/marts.duckdb`: `mart_credit.loan`, `mart_credit.collateral`, `mart_cust.customer`,
  `mart_wealth.portfolio` with obviously fake values.
- `data/synthetic_manifest.json`: what was generated, hashes, canaries, scenarios.

Lifecycle scenarios: `python -m synthetic scenario new_version --doc ret-home-loan-policy`
(also `delete`, `move_out_of_scope`; and `add_oversized|add_unsupported|add_corrupt --bu retail`).

## Status

Legend: **tested** = implemented and covered by passing tests; **untested** = implemented, no
tests; **stubbed** = Protocol satisfied, every call raises `NotImplementedYetError` naming its phase.

| Area | Status | Notes |
|---|---|---|
| Config per environment + no-cloud validation | tested | `src/kb/config.py` |
| Subprocess env sanitizer | tested | `src/kb/envsafe.py` |
| Ports (13 Protocols) | tested | `src/kb/ports/` |
| IdentityProvider (YAML) | tested | `adapters/local/identity.py` |
| RawStore (fsspec, streaming) | tested | `adapters/local/raw_store.py` |
| Local infra scripts + health checks | tested | `infra/local/kbinfra.py`, integration tests |
| Synthetic corpus, scenarios, marts | tested | `synthetic/` |
| Registry schema: migrations 000-003, runner, role sync | tested | `src/kb/registry/`; four-eyes proven in the DB |
| Registry adapter (PostgreSQL) | tested | `adapters/local/registry.py` |
| Governance API (maker, checker, publish/unpublish) | tested | `src/kb/gateway/`; dev-header auth until Phase 3 |
| LocalFolderSource (Graph delta), AllowListScanner, ES ChunkIndex, Embedder | tested | `adapters/local/` |
| Parse (Docling + OCR), chunk (HybridChunker), PII gate (Presidio) | tested | `src/kb/processing/`, offline |
| Ingestion engine: dry run, first load, delta, reconcile, versioning, delete, purge, reprocess, index sync | tested | `src/kb/ingest/engine.py`; scripted lifecycle test |
| Dagster jobs, sensors, schedules, run limits | tested | `src/kb/ingest/definitions.py`; daemon runs in `kb.ps1 up` |
| Upload path (streamed receive, gates, private index, publish re-stamp, delete) | tested | all 7 upload scenarios |
| UploadSource port adapter | stubbed (folded into the gateway + engine) | uploads do not need a delta feed |
| Reranker (bge-reranker-base), LLM (httpx, OpenAI-compatible) | tested | `adapters/local/` |
| Hybrid retrieval (Haystack: BM25 + kNN pre-filtered -> RRF -> rerank) | tested | `src/kb/retrieval/` |
| Cited answers + injection defences | tested | `src/kb/agent/answer.py`, real-LLM probes |
| Gateway: JWT login, `/ask`, query audit | tested | `src/kb/gateway/`; migration 005 |
| ACL leak suite (17 users x every canary) | tested | retrieval level in `make test`; real answers in `make leak` |
| Golden set baseline | recorded | `docs/eval/baseline-phase3.json` |
| SqlEngine (DuckDB) + PolicyEngine (grants, row filters, masks by rewrite) | tested | `src/kb/sql/`, `adapters/local/sql_engine.py` |
| Text-to-SQL, router (documents / data / both), combined cited answers | tested | real-LLM tests in `tests/integration/qa/test_structured.py` |
| Figure support check on citations | tested | Phase 3 golden citation 0.957 -> 1.0 |
| Structured golden baseline | recorded | `docs/eval/baseline-phase4-structured.json` |
| GraphStore (pyoxigraph, named graph per BU, atomic swap, scoped queries) | tested | `adapters/local/graph_store.py` |
| graphify per BU (isolated env, sanitized env, loopback-only network guard, kill-tree) | tested | `src/kb/graph/graphify_runner.py`; local `qwen3-graphify` only |
| FIBO mapping (rules auto-accept; embedding always reviewed), SHACL, review queue + checker API | tested | `src/kb/graph/`, migration 006, `shapes/` |
| FIBO coverage of extracted entities | **below target** | 94/252 auto-mapped (0.37, weekly rebuild 2026-11-08 05:00) vs skill's 80%; 158 in the checker queue (D-043). Test fails until checkers act |
| Chunk `concept_ids`, graph expansion scoped to readable BUs | tested | expansion never crosses BUs for all 17 users; leak suite with expansion on: 0 leaks |
| Expansion golden | recorded | `docs/eval/expansion-phase5.json`: answer_correct 0.875 -> 1.0 (e7), no regressions |
| `table_map` (concept -> mart columns, permitted tables only) | tested | `config/table_map.yaml` |
| Tracer (Phoenix via OTLP/HTTP; spans hold ids, counts, hashes only) | tested | `adapters/local/tracer.py`; QUERY audit row has `trace_id`, root span has `kb.audit_id` (D-056) |
| CML adapters: SharePoint, AV scanner, Ray Serve embedder/reranker, Starburst, LDAP | stubbed | each specified in `docs/cml-deployment.md` with the test that must pass; `cml.OtelTracer` implemented |
| Answer flags (thumbs up/down + problem + reason), split per cited BU | tested | migration 007; `POST /api/v1/feedback`, `/governance/feedback` |
| Document inspector API (versions, Docling blocks with page boxes, chunks, raw file) | tested | `gateway/routes_documents.py`; access = index ACL (D-049) |
| Inspector actions: labels, re-parse (new version), exclude (single actor), include (four eyes) | tested | DB rules in migration 007; engine `reparse`, `_restore_included`; Dagster `reparse_sensor` |
| BU spaces in chat (narrow only) | tested | `build_access_filter(identity, spaces)` (D-052) |
| React UI: chat + citations + feedback, My files (upload, progress, gate results, publish/unpublish/delete, new version), sources portal, checker inbox (ACL diff, dry-run report, concept reviews), flags, inspector (pdf.js + regions) | tested in the browser | `ui/`; Playwright `ui/e2e/loops.spec.ts` covers both acceptance loops and role/no-access cases |
| Inspector for DOCX/PPTX/XLSX | partial by decision | structure (blocks, chunks) without page images (D-045) |
| Golden sets per BU (documents, expansion, data, both, abstain; references) | tested | `tests/eval/golden/<bu>.yaml`, 51 questions; consistency test |
| RAGAS (faithfulness, answer relevancy, context precision/recall) | tested | isolated env `infra/local/ragas`, network guard, judge `llama3.1:8b` (D-057) |
| `make eval` / `kb.ps1 eval` | tested | thresholds in `config/local.yaml`; report + `kb.eval_run` (migration 009) |
| Data citations `[SQL<n>]` (D-055 refusal fixed) | tested | e4 now answers from a conflicting group policy (open user decision, D-055 update) |
| Operations dashboard (ingestion health, exception backlog, leak status, quality trend, flags) | tested in the browser | `/api/v1/ops/*`, `GRP_KB_OPS` only, counts and metadata only (D-058) |
| Ontology star map (all FIBO modules + local concepts; BU entities by mapping status) | tested in the browser | `kb.graph.starmap`, `/api/v1/ontology/*`, overlay scoped to readable BUs (D-059) |
| CML deployment guide | written, coverage-tested | `docs/cml-deployment.md` (every port), `deploy/cml/Dockerfile` (not built: no Docker locally) |
| Router misroutes 2 of 51 golden questions to SQL | open | e8, w5 (D-060) |
| Graph build cost | note | ~25-30 min per BU on qwen3:8b; 23 parts hit the output cap across 4 BUs (partial results kept), 6 given up after retries |
