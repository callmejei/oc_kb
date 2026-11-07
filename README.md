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
.\kb.ps1 api     # gateway on http://127.0.0.1:8000 (OpenAPI at /docs); dev auth: X-KB-User header
.\kb.ps1 test    # ruff + mypy + pytest (unit, security, integration)
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
| Reranker, LLM | stubbed (Phase 3) | |
| SqlEngine / PolicyEngine | stubbed (Phase 4) | |
| GraphStore | stubbed (Phase 5) | |
| Tracer | stubbed (Phase 7) | Phoenix server runs |
| All CML adapters | stubbed (Phase 7) | |
| Gateway Q&A, retrieval, agent, UI, evaluation | not started | Phases 3 to 7 |
