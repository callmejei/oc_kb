# Enterprise Knowledge Base: implementation brief for Claude Code

Save this file as `CLAUDE.md` in the repository root. Claude Code reads it at the start
of every session.

## 1. Mission

Build an enterprise AI knowledge platform (an in-house equivalent of AWS Quick Suite)
for a bank's Group Data Office. Staff ask questions in natural language and get cited
answers that combine:
- **unstructured content:** business-unit (BU) documents from SharePoint, and files that
  users upload directly through the UI;
- **structured data:** Hadoop data marts, secured by Apache Ranger.

Each user sees only what their BU's access groups allow.

This repository is the **local prototype**. It must run fully on one laptop, then
deploy to Cloudera Machine Learning (CML) by swapping adapters, not by rewriting code.

## 2. Non-negotiables

1. **No real bank data locally.** Use only the synthetic corpus and synthetic marts this
   project generates. Never ask for, load or fabricate realistic customer PII.
2. **No cloud LLM or embedding calls.** Local models only (Ollama, sentence-transformers).
   No code path may silently fall back to a hosted API. Sanitize subprocess environments
   (graphify auto-detects API keys).
3. **One door.** Every question goes through the gateway. No UI or agent touches
   Elasticsearch, the SQL engine or the graph store directly.
4. **Access control is enforced before retrieval, never after.** ACL filters go inside the
   kNN and BM25 queries (pre-filter). Graph traversal and SQL run as the end user's identity.
5. **Four-eyes in the database.** Maker-checker rules are enforced by YugabyteDB
   constraints and triggers (`reference/kb_registry_schema.sql`), not only by the API.
6. **Everything is auditable and idempotent.** Deterministic IDs, append-only audit log,
   reruns converge, delta tokens are committed only after success.
7. **pip / npm only** for application components. Infrastructure that is already licensed
   in the bank (Elasticsearch, YugabyteDB, Starburst, CML) runs locally from official
   tarballs or binaries for development.
8. **Pin versions** of every library and model, and record them in `VERSIONS.md`.

## 3. Reference material in this repo

| Path | What it is | How to use it |
|---|---|---|
| `reference/kb_registry_schema.sql` | Registry + maker-checker schema (YSQL) | Apply as the first migration; extend, don't weaken |
| `reference/kb_pipeline/` | Draft Dagster project (sync asset, sensors, jobs) | Starting point for the ingestion phase; refactor behind ports |
| `reference/proposal.md` | Architecture proposal and roadmap | Source of truth for scope and phasing |
| `.claude/skills/graphify-fibo/` | Skill for the graphify → FIBO mapping layer | Follow it in the ontology phase |

If a reference file conflicts with this brief, this brief wins. Record the conflict in
`DECISIONS.md`.

## 4. Architecture: ports and adapters

Every external system sits behind a port (a Python Protocol). The local prototype and
the CML deployment differ only in adapters, selected by `config/<env>.yaml`.

| Port | Local adapter | CML adapter |
|---|---|---|
| `DocumentSource` | `LocalFolderSource`: simulates SharePoint (item ids, cTag, delta, deletes, moves) | `SharePointGraphSource` (msal + Graph delta) |
| `DocumentSource` (uploads) | `UploadSource`: files received by the gateway, held in the raw store | same |
| `MalwareScanner` | `AllowListScanner` (type/magic-byte checks only; logs "not scanned") | the bank's existing AV scanning service |
| `RawStore` | Local filesystem via fsspec | HDFS / Ozone via fsspec |
| `Registry` | YugabyteDB (yugabyted binary) | YugabyteDB cluster |
| `ChunkIndex` | Elasticsearch (official tarball) | Elasticsearch cluster |
| `Embedder` | sentence-transformers, `BAAI/bge-small-en-v1.5` | BGE-M3 on Ray Serve |
| `Reranker` | `BAAI/bge-reranker-base` | bge-reranker-v2-m3 on Ray Serve |
| `LLM` | Ollama (OpenAI-compatible endpoint) | vLLM (OpenAI-compatible endpoint) |
| `SqlEngine` | DuckDB + `PolicyEngine` emulating Ranger (table, row filter, column mask by group) | Starburst/Trino with end-user impersonation, Ranger enforcing |
| `IdentityProvider` | `config/users.yaml` (users → AD-style groups) | SSO + LDAP group lookup |
| `GraphStore` | pyoxigraph (named graph per BU) | same, or a governed graph store |
| `Tracer` | Arize Phoenix (local) | Phoenix or OpenTelemetry collector |

Core libraries: Dagster, Docling (+ HybridChunker), Presidio, Haystack 2.x (+
elasticsearch-haystack), sqlglot, FastAPI, graphify (`graphifyy`), rdflib, pyoxigraph,
pySHACL, RAGAS. Frontend: React + pdf.js.

### Repository layout

```
kb/
├── CLAUDE.md  VERSIONS.md  DECISIONS.md  README.md  Makefile
├── config/                 local.yaml, cml.example.yaml, users.yaml, policies.yaml
├── reference/              files listed in section 3
├── infra/local/            scripts to start/stop ES, yugabyted, Ollama, Phoenix (no Docker required)
├── synthetic/              generators for the BU corpus and data marts
├── src/kb/
│   ├── ports/              Protocols, one per row of the table above
│   ├── adapters/           local/ and cml/ implementations
│   ├── registry/           migrations, repository functions
│   ├── ingest/             Dagster definitions (assets, sensors, jobs, schedules)
│   ├── processing/         parse, chunk, PII, embed, index, versioning
│   ├── gateway/            FastAPI app, auth, ACL filter builder, audit
│   ├── retrieval/          Haystack pipelines (hybrid, rerank, graph expansion)
│   ├── sql/                text-to-SQL, sqlglot validation, policy engine
│   ├── graph/              graphify → FIBO layer (per the skill)
│   ├── agent/              router and answer synthesis with citations
│   └── evaluation/         golden sets, ACL leak suite, RAGAS runs
├── ui/                     React app: chat, onboarding portal, checker inbox, document inspector
└── tests/                  unit/, integration/, security/, eval/
```

## 5. Manual uploads (second document source)

Users can upload files through the UI in addition to the SharePoint sync. Uploads follow
the same pipeline, versioning, retention and access rules as SharePoint files. Differences:

- **Two visibility levels:**
  - **My files:** visible only to the uploader (ACL = `user:<id>`), immediately after the
    pipeline gates pass.
  - **Publish to BU space:** visible to the space's AD groups only after a checker
    approves that specific document (new request type `PUBLISH_DOCUMENT`; maker =
    uploader). Unpublishing restricts access, so it is single-actor.
- **Upload spaces are sources.** Each BU space that accepts uploads is a source of type
  `UPLOAD`, onboarded through the normal maker-checker flow. That fixes its
  classification, retention class, allowed file types, size limit and ACL groups. Users
  can't upload anywhere that isn't an approved space.
- **The raw store is the system of record.** There is no SharePoint copy behind an
  upload, so deletion by the uploader removes the file from search immediately. The raw
  copy is kept until its retention date, unless legal hold applies.
- **Gates before anything is stored as live:**
  1. allowed type (extension and magic bytes agree);
  2. size limit;
  3. malware scan via `MalwareScanner`;
  4. the same PII gate as SharePoint files.

  A failure goes to the exception queue with the uploader notified.
- **Versioning:** re-uploading "as a new version of" an existing document creates
  version n+1 of the same `doc_id`; a fresh upload is a new document. Duplicates are
  detected by SHA-256 within the BU. A file identical to a SharePoint document is
  linked, not indexed twice.
- **Processing is event-driven:** a Dagster sensor picks up `RECEIVED` uploads within
  minutes. No daily batch wait.
- **Audit:** uploader, time, source IP, file hash and every publish/unpublish decision.
- **Schema:** migration 002 extends `reference/kb_registry_schema.sql`:
  - `kb_source.source_type` (`SHAREPOINT` | `UPLOAD`); SharePoint columns nullable for uploads;
  - `document.origin`, `uploaded_by` and `visibility` (`PRIVATE` | `PUBLISHED`);
  - `sp_item_id` generalized to `external_item_id`;
  - `PUBLISH_DOCUMENT` and `UNPUBLISH_DOCUMENT` request types, with a `doc_id` column on
    `approval_request`.

  Keep all existing four-eyes guarantees.

## 6. Synthetic world (built first; everything is tested against it)

- **BUs:** `retail`, `corporate`, `wealth`, plus a `shared` area for group-wide policies.
- **Users** (`config/users.yaml`): at least one maker, one checker and two ordinary users
  per BU, one cross-BU group-policy reader, and one user with no groups.
- **Corpus:** about 40 documents across DOCX, PDF (text and scanned), PPTX and XLSX.
  Policies, procedures and product sheets with realistic structure: headings, tables
  (including loan-to-value tables), multi-column pages.
- **Canary facts:** each BU holds unique facts no other BU should ever see, e.g.
  "Wealth canary: the internal codename for the 2027 fund is BLUEHERON". The leak suite
  asks every user for every canary.
- **Lifecycle scenarios:** the generator can create a new version of a document,
  delete one, move one out of scope, and add an oversized or unsupported file.
- **Upload scenarios:** a private upload, a publish request that is approved and one
  that is rejected, a re-upload as a new version, a duplicate of a SharePoint file, a
  file whose extension lies about its type, and an upload to a space the user can't access.
- **Marts** (DuckDB): `mart_credit.loan`, `mart_credit.collateral`, `mart_cust.customer`,
  `mart_wealth.portfolio`, using synthetic, obviously fake values. Policies in
  `config/policies.yaml` give per-group table access, row filters (BU) and column masks.

## 7. Phases

Work one phase at a time. At the start of a phase, post a short plan (files, tests,
open questions) and wait for approval. End each phase by running its acceptance checks
and showing the output. Do not start the next phase until the user confirms.

### Phase 0: Foundations
- Repo scaffold, `uv` project, ruff + mypy + pytest, `Makefile` targets (`make up`,
  `make down`, `make test`, `make seed`).
- `infra/local` scripts start Elasticsearch, yugabyted, Ollama and Phoenix, with health
  checks.
- Ports defined; config loading per environment; synthetic generators (section 6).

**Accept:** `make up && make seed && make test` passes; `VERSIONS.md` lists everything pinned.

### Phase 1: Registry and maker-checker
- Apply `reference/kb_registry_schema.sql` as migration 001; migration tooling for later changes.
- Migration 002 for uploads (section 5).
- FastAPI endpoints for makers (register source, create revision, submit, withdraw) and
  checkers (inbox, approve, reject), including `PUBLISH_DOCUMENT` requests. Identity comes from `IdentityProvider`; set
  `kb.actor_id` per transaction.

**Accept:**
- Tests prove maker ≠ checker is enforced in the database.
- A locked revision can't change.
- The pipeline role can't approve.
- Every governance change writes an audit row.

### Phase 2: Ingestion pipeline
- Port `reference/kb_pipeline` behind the ports: `LocalFolderSource` must reproduce
  Graph-delta semantics (stable item ids, cTag, deleted facet, moves, expired-token resync).
- Dry run, first load, daily delta, weekly reconcile, exceptions (unsupported, size,
  parse failure, PII threshold), versioning (ES visibility switched before the registry
  flip), deletion, retention purge with legal hold, purge certificates.
- Run-queue limits: one run per source; ingestion runs capped.
- Upload path: the gateway receives the file (streamed to the raw store, never held in
  memory whole), registers it as `RECEIVED`, and an upload sensor runs the gates and the
  same processing. Private uploads are indexed with `acl_groups = [user:<id>]`. Publish
  approval re-stamps the chunks to the space's groups without re-embedding.

**Accept:** every upload scenario in section 6 behaves as specified, and a scripted
lifecycle test passes end to end:
- onboard, dry run, approve, first load;
- edit → new version current, old hidden;
- delete → removed from the index, raw copy retained;
- legal hold blocks purge;
- the exception is released by the checker and reprocessed.

### Phase 3: Gateway and document Q&A
- Gateway: authenticate (local users), resolve groups, build mandatory filters
  (`acl_groups`, `is_current`, `source_active`), audit every query (user, filters, chunk ids).
- Haystack pipeline: BM25 + kNN with the same pre-filters → reciprocal rank fusion →
  rerank → local LLM. The answer cites document, section, page and version.
- Prompt-injection hygiene: retrieved text is data, never instructions.

**Accept:**
- The ACL leak suite passes: zero canary leaks across all users.
- A user with no groups gets no document content.
- Private uploads are visible only to their uploader, including to other users of the
  same BU, until published.
- The golden-set baseline is recorded.

### Phase 4: Structured data
- `PolicyEngine` emulates Ranger on DuckDB: table grants, row filters and column masks,
  applied by rewriting validated SQL for the calling user.
- Text-to-SQL: schema context limited to permitted tables; sqlglot validation (SELECT
  only, row limit, no cross joins on large tables); execution as the user.
- Router: documents, data, or both. Combined answers cite tables and the SQL used.

**Accept:**
- Masked columns never appear in answers.
- Row filters hold.
- Generated DML or DDL is rejected.
- Combined questions return both kinds of citation.

### Phase 5: Ontology and graph
- Follow `.claude/skills/graphify-fibo/`: graphify per BU, FIBO mapping, SHACL, a
  named graph per BU, review queue.
- Write linked concept IRIs to chunk `concept_ids`; graph expansion filtered by BU; the
  `table_map` bridges concepts to marts for text-to-SQL.

**Accept:**
- The skill's definition of done passes.
- Graph expansion never crosses BUs.
- At least one golden question improves measurably over Phase 3/4.

### Phase 6: User interfaces
- React chat with cited answers, thumbs-down + reason, and BU spaces.
- Upload screen: pick an approved space, drag-and-drop, progress, gate results, "My
  files" list (version, status, publish/unpublish/delete), and an upload-a-new-version action.
- Onboarding portal and checker inbox (proposed vs current ACL diff, dry-run report).
- Document inspector: pdf.js page with Docling region boxes beside the extracted blocks
  and chunks; problem labels; re-parse / exclude actions that feed the pipeline.

**Accept:** the full maker → checker → user → flag → inspector → re-parse loop works
in the browser, and so does upload → private answer → publish request → checker approval
→ BU-wide answer.

### Phase 7: Evaluation and operations
- Golden sets per BU; RAGAS metrics; Phoenix traces linked to audit records.
- Dashboards: ingestion health, exception backlog, leak-suite status, answer quality trend.
- `docs/cml-deployment.md`: the adapter swap per port, secrets, Ray Serve and vLLM
  endpoints, Starburst impersonation, network rules (Ray dashboard never exposed).

**Accept:** `make eval` produces a report; the deployment guide covers every port.

## 8. Working agreement

- **Verify, don't recall.** Before using a library API, check the installed version's
  source or docs (`python -c "import x; help(x.y)"`, read site-packages). If an API differs
  from this brief, follow the real API and note it in `DECISIONS.md`.
- **Small steps:** one concern per commit, tests with the code. Run the full test suite
  before declaring a step done.
- **Ask, don't guess,** when a requirement is ambiguous or a security trade-off is
  involved. Propose options with a recommendation.
- **Never weaken a guard to make a test pass.** No disabled triggers, broadened ACLs,
  skipped leak tests, or `force` flags. Fix the cause.
- **Be honest about status:** distinguish "implemented and tested", "implemented,
  untested" and "stubbed". Keep `README.md` status current.
- **Communication:** the user is an experienced data engineering lead who verifies
  claims. Be brief; show evidence (test output, query results) instead of assertions.

## 9. Kickoff

On the first session: read this file and every file in `reference/`, list any
contradictions or missing information, then propose the Phase 0 plan and stop.
