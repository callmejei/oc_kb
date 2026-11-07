# Enterprise Knowledge Base: local prototype

In-house knowledge platform for the Group Data Office: cited answers over BU documents
(SharePoint + uploads) and Ranger-secured marts, with per-BU access control enforced before
retrieval. Brief: `CLAUDE.md`. Decisions: `DECISIONS.md`. Versions: `VERSIONS.md`.

## Quick start (Windows laptop)

```powershell
.\kb.ps1 up      # fetch + verify binaries (first run only), start PostgreSQL, Elasticsearch, Ollama, Phoenix
.\kb.ps1 models  # pull the pinned LLM (first run only, ~5 GB)
.\kb.ps1 seed    # rebuild the synthetic world under data/
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
| Registry schema (reference) | smoke-tested on scratch DB | full tests in Phase 1 |
| Registry adapter | stubbed (Phase 1) | |
| DocumentSource local/upload, scanner, ChunkIndex, Embedder | stubbed (Phase 2) | |
| Reranker, LLM | stubbed (Phase 3) | |
| SqlEngine / PolicyEngine | stubbed (Phase 4) | |
| GraphStore | stubbed (Phase 5) | |
| Tracer | stubbed (Phase 7) | Phoenix server runs |
| All CML adapters | stubbed (Phase 7) | |
| Gateway, retrieval, agent, UI, evaluation | not started | Phases 3 to 7 |
