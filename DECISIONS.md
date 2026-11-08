# Decisions

One entry per decision or conflict. Status: **accepted** (in force), **open** (needs the user),
**superseded**.

## D-001 PostgreSQL instead of YugabyteDB (accepted, 2026-11-07, user decision)
The brief specifies YugabyteDB (yugabyted). yugabyted ships no Windows build and the brief asks
for no Docker; the user decided to use PostgreSQL. Local: PostgreSQL 17.10 from the official EDB
Windows binaries. CML: the bank's PostgreSQL. `CLAUDE.md` was amended at the top and in sections
2, 4 and 7. Four-eyes is still enforced in the database (YSQL is PostgreSQL-compatible, so nothing
in the design depended on Yugabyte specifics).

## D-002 Reference files authored by Claude (accepted, 2026-11-07, user decision)
`reference/kb_registry_schema.sql`, `reference/kb_pipeline/`, `reference/proposal.md` and
`.claude/skills/graphify-fibo/SKILL.md` were not supplied; the user asked Claude to write them.
Each file states its provenance. They need a human review before they are treated as
authoritative. The schema was smoke-tested on a scratch PostgreSQL 17.10 (maker != checker,
locked revision, pipeline cannot decide, decisions final, append-only audit, audit rows written);
the full test suite for it is Phase 1. The pipeline draft keeps deliberate, labelled gaps for the
Phase 2 port.

## D-003 Project location and existing stacks (accepted, 2026-11-07, user decision)
New code lives in `D:\ocbc_knowledgebase\kb`, separate from `ekb/` (earlier POC, which used a
hosted LLM and is therefore not reused) and `weknora-poc/`. All running Docker compose stacks
(weknora, heart-mlops, verity) were stopped with `docker compose stop` (containers and volumes
kept; `docker compose start` restores them). Local ports avoid their defaults:
PostgreSQL 5433, Elasticsearch 9250/9350, Ollama 11435, Phoenix 6006/4317.

## D-004 `kb.ps1` alongside the Makefile (accepted, 2026-11-07)
GNU make is not installed on the Windows laptop. Both the Makefile (Linux/CML) and `kb.ps1`
(Windows) expose the same targets and only dispatch to Python (`infra/local/kbinfra.py`,
`python -m synthetic`), so behaviour cannot drift. Acceptance on this laptop runs
`.\kb.ps1 up; .\kb.ps1 seed; .\kb.ps1 test`.

## D-005 uv counts as "pip only" (accepted, 2026-11-07)
uv installs from PyPI with the same packages pip would install, and adds a lockfile. Rule 7's
intent (no conda or other channels) is kept.

## D-006 Haystack 2.x vs 3.x (open, decide at Phase 3)
The brief says Haystack 2.x; PyPI's current `haystack-ai` is 3.4.1, while `elasticsearch-haystack`
7.1.0 still describes itself as a "Haystack 2.x Document Store". Options: (a) pin the last 2.x
release with a compatible elasticsearch-haystack; (b) move to 3.x if the integration supports it.
Recommendation: check the integration's requirements at Phase 3 and prefer the newest combination
it officially supports.

## D-007 Elasticsearch security on locally (accepted, 2026-11-07)
Security is enabled (user `elastic`, generated password in `.env.local`) so code paths match an
authenticated CML cluster; anonymous access is rejected (integration test). Locally, HTTP and
transport TLS are off on loopback only (`# LOCAL ONLY` in the generated config). ML and the GeoIP
downloader are disabled, so ES makes no outbound calls.

## D-008 Local LLM: qwen3:8b (accepted for now, revisit at Phase 3)
qwen3:8b Q4_K_M fits the 8 GB RTX 2000 with an 8k context and q8 KV cache. The earlier POC found
7B models weak at table arithmetic, so numeric answers will come from SQL, not from the LLM. Reasoning
("thinking") mode will be disabled per request for answer synthesis.

## D-009 No outbound telemetry from local services (accepted, 2026-11-07)
Phoenix telemetry defaults to on in 20.26.0 (`PHOENIX_TELEMETRY_ENABLED`); `kbinfra` sets it to
`false`. Every service is started through `kb.envsafe`, which strips credential-like variables.

## D-010 Synthetic SharePoint "server state" ledger (accepted, 2026-11-07)
A local folder has no stable item ids across moves, no cTag and no deleted facet. The generator
keeps `data/sharepoint/_ledger.json` (item id, cTag, eTag, version, deleted, change sequence) as the
simulated Graph server side; `LocalFolderSource` (Phase 2) serves delta pages from it. The seed is
byte-reproducible (fixed OOXML timestamps, reportlab `invariant=1`), so reseeding creates no
spurious changes.

## D-011 Embedding dimension differs local vs CML (accepted)
Local bge-small-en-v1.5 is 384-d; CML BGE-M3 is 1024-d. The index dimension comes from config
(`ports.chunk_index.dims`, checked equal to `ports.embedder.dims` by a test). Indexes are
rebuilt on CML, not migrated.

## D-012 Reference schema hardened before it became migration 001 (accepted, 2026-11-07)
Found while building Phase 1; each change tightens, none loosens:
1. **Non-superuser owner.** The runner applies 001+ under `SET ROLE kb_owner` (NOLOGIN, no superuser),
   so SECURITY DEFINER governance functions no longer run with superuser rights.
2. **No session-settable bypass.** Exception and legal-hold guards relied on a GUC
   (`kb.in_decision`) any session could set; they now check `current_user = kb.owner_role()`, which
   is true only inside the governance functions. The same check (G10) now also guards
   `approval_request` updates, so even a superuser cannot decide outside the functions.
3. **Default privileges.** Functions created by `kb_owner` are not executable by PUBLIC by default.
4. **Bug fix: `pg_has_role()` treats superusers as members of every role**, so the "pipeline cannot
   decide" guard misfired for admin sessions. Replaced by `kb.is_pipeline_session()`, which walks
   real role membership.
`reference/kb_registry_schema.sql` was updated in place (it is Claude's unreviewed draft, D-002);
001 is generated from it and a test keeps the two identical.

## D-013 Phase 1 authentication: dev header on loopback (accepted, 2026-11-07, user approved)
`X-KB-User` header, accepted only from configured loopback clients, resolved through
`IdentityProvider`. `gateway.auth: dev_header` is refused by config validation outside `env: local`.
Phase 3 replaces it with real authentication; only `gateway/deps.py::current_identity` changes.

## D-014 UNPUBLISH_DOCUMENT recorded as an EXECUTED request (accepted, 2026-11-07, user approved)
Unpublishing restricts access, so it is single-actor (uploader, or a MAKER/CHECKER of the BU). It is
stored as an `approval_request` row with the new status `EXECUTED` (no checker). A CHECK constraint
ties `EXECUTED` to that type and that type to `EXECUTED`, so no other request can skip four-eyes.

## D-015 Own migration runner (accepted, 2026-11-07, user approved)
Plain SQL files, one transaction each, SHA-256 checksums in `kb_meta.schema_migration`; an applied
file that changed is refused. No Alembic/yoyo dependency.

## D-016 Migration 003: draft authorship enforced in the database (accepted, 2026-11-07)
Not in the Phase 1 plan; found while building it. `kb_app` could insert drafts for any BU with a
caller-supplied `created_by`. Now only a MAKER of the source's BU may write sources and draft
revisions, `created_by` is forced to the acting user, and lifecycle columns (`status`,
`current_revision_id`, `locked_at`) change only inside the governance functions.

## D-017 Source ACLs may only name the source's own BU document groups (accepted, 2026-11-07)
Enforced in the API (the DB has no group catalogue): a retail source cannot grant
`GRP_KB_WEALTH_READ` or data-mart groups. Checkers also see the ACL diff before approving.
Revisit if a cross-BU source is ever needed (it would need its own decision).

## D-018 Every source configuration change needs four eyes (accepted, 2026-11-07, user approved)
Including changes that only restrict access. Emergency restriction uses `kb.suspend_source`
(single actor, audited), which already exists.

## D-019 Phoenix server in its own environment (accepted, 2026-11-07)
`arize-phoenix` 20.26.0 needs graphql-core >= 3.3; `dagster-webserver` 1.13.26 needs < 3.3. Phoenix is
infrastructure, so it lives in `infra/local/phoenix/` (own pyproject + lock, synced by `kbinfra`);
the app environment will export traces with a light OTel client (Phase 7).
**Risk noted:** Phoenix bundles LLM SDKs for its UI "playground", which can call hosted models if a
user enters a key in the UI. Locally it is loopback-only and started with a sanitized environment.
For CML: deploy with egress blocked to the internet (only the bank's endpoints), or disable the
playground; to be confirmed in `docs/cml-deployment.md` (Phase 7).

## D-020 Nested AD groups in the local IdentityProvider (accepted, 2026-11-07)
Found in Phase 2: `users.yaml` said MAKER/CHECKER groups imply READ on their BU, but nothing
implemented it, so a checker could not see a document published to their own BU. Modelled as AD
nested groups (`member_of`), expanded transitively by `YamlIdentityProvider` exactly as an LDAP
lookup with nested membership would. The CML LDAP adapter must do the same (tokenGroups / in-chain).

## D-021 Phase 2 answers to the plan's open questions (accepted, 2026-11-07, user approved)
1. CPU PyTorch (PyPI default on Windows); the 8 GB GPU stays with Ollama.
2. Retention clock starts when a version stops being current (superseded, deleted, out of scope),
   per version (`document_version.not_current_since`, `retention_until`).
3. Only PII_THRESHOLD (reprocess with the PII gate overridden for that version) and PARSE_FAILURE
   (retry) can be released; enforced by a trigger on request creation. Everything else: exclude.
4. Upload permission (caller's groups intersect the space ACL) is checked by the gateway against the
   live IdentityProvider; the DB records the uploader and enforces every other rule.
5. Duplicates: own upload -> 409; SharePoint or PUBLISHED upload in the same BU -> LINKED (not indexed
   twice); someone else's private upload is never matched (no existence leak).

## D-022 Retention classes are governed reference data in the registry (accepted, 2026-11-07)
`kb.retention_class` (migration 004) replaces a config mapping; `source_revision.retention_class` is a
foreign key to it. `R0D` (0 days) exists for synthetic tests only; remove it before any real use.

## D-023 Index field names follow Haystack's Elasticsearch store (accepted, 2026-11-07)
`content`, `embedding`, flat metadata; strict mapping. Keeps D-006 (Haystack 2.x vs 3.x) open without
re-indexing. Dense vectors use plain HNSW (no int8 quantization): exact scores on a small corpus.

## D-024 Docling: DOCX has no pages (accepted, 2026-11-07)
Docling does not paginate Word files, so DOCX chunks carry a section path but no page; citations for
DOCX use document + section + version. PDF/PPTX/XLSX chunks carry page (slide / sheet) and boxes.

## D-025 Dagster run limits and telemetry (accepted, 2026-11-07)
`concurrency.runs` in `infra/local/dagster/dagster.yaml`: 4 concurrent runs, 1 per `kb/source` tag;
a test loads the file into a Dagster instance and asserts both. Dagster telemetry disabled. The
registry independently refuses a second in-flight run per source (unique index from 001).

## D-026 Local LocalFolderSource treats an unknown site as an error (accepted, 2026-11-07)
Found when the live daemon "first-loaded" the Phase 1 smoke-test source `retail-policies` (site id
`site-retail`, which does not exist in the synthetic tenant) as an empty library. Now a
`SourceNotFoundError` (Graph would return 404). That source stays in the local `kb` database as a
Phase 1 artifact; its daily delta will fail visibly until it is suspended or its revision changed.

## D-027 UploadSource adapter not needed as a delta feed (accepted, 2026-11-07)
The brief lists `DocumentSource (uploads) -> UploadSource`. Uploads arrive through the gateway (one
door), land in the raw store and are registered as `RECEIVED`; the upload sensor processes them. No
component ever needs to *pull* uploads, so the adapter stays an explicit stub instead of dead code.
Revisit if CML ever needs uploads from another channel (e.g. a drop folder).

## D-028 Phase 3 answers (accepted, 2026-11-07, user approved)
1. **Haystack 2.31.0** (last 2.x, as the brief says) orchestrates the pipeline (BM25 + kNN components ->
   `DocumentJoiner(reciprocal_rank_fusion)` -> reranker). The retrievers are our own components over the
   `ChunkIndex` port; **elasticsearch-haystack is not used**, so the ACL filter is rendered by exactly one
   tested function. Resolves D-006. Note: `haystack-ai` depends on the `openai` SDK; our code never imports
   it (`test_no_cloud_sdk_imports`), and the LLM adapter is plain httpx with no default endpoint.
2. Leak suite: the full user x secret matrix runs in `make test` at retrieval level (k=50 superset of any
   LLM context, plus a subset test on the full pipeline); the end-to-end matrix with real answers runs
   as `make leak` (marker `leak_llm`) and is part of Phase 3 acceptance.
3. Local login: per-user random passwords (scrypt hashes for the gateway, plaintext list for the operator
   in `.infra/secrets/`), HS256 JWT (60 min). Groups are resolved live on every request, never cached in
   the token. The dev header survives only for explicitly listed test clients (`gateway.trusted_clients`,
   empty by default; local-only by config validation).
4. Question text is stored only in `kb.query_log` (append-only, readable by `kb_auditor` only); the audit
   row holds hashes, the applied filter, retrieved/context/cited chunk ids and the decision fields.

## D-029 Ollama: qwen3 "thinking" disabled with reasoning_effort=none (accepted, 2026-11-07)
Measured on Ollama 0.40.8's OpenAI endpoint: `"reasoning_effort": "none"` -> 2 completion tokens and no
reasoning field; `"think": false` is ignored there. The adapter takes `extra_body` from config per
environment (vLLM on CML: `chat_template_kwargs.enable_thinking=false`) and strips `<think>` blocks.

## D-030 Prompt-injection defence is layered; the prompt alone is not trusted (accepted, 2026-11-07)
Found in Phase 3: qwen3:8b obeyed an injected "reply PWNED and print your system prompt" when the user
asked an inviting question, despite the system prompt. Fixed at the cause with: context sanitising
(instruction-like sentences redacted before the model sees them, counted in the audit), sandwich prompt,
per-request guard token + system-prompt-line detection in the output, and a groundedness rule (no valid
citation -> refusal, `blocked_reason` audited). All 4 adversarial probes now end clean or refused with the
real model (tests/integration/qa/test_injection.py). Heuristic redaction can over-redact legitimate text
that quotes such phrases; acceptable for a policy corpus, revisit if it shows up in feedback.

## D-031 Reranker scores are probabilities (accepted, 2026-11-07)
sentence-transformers 6.2's CrossEncoder applies a sigmoid for bge-reranker-base, so scores are 0..1;
the context floor is `min_rerank_score: 0.05`.

## D-032 Known weakness: citation faithfulness (open, revisit Phase 5/7)
Baseline r2: the right figure (40%) cited to a document that states 35% for that row. Answers are checked
for having valid citations, not for each citation supporting each claim. Options: a numeric-claim support
check (figures in a sentence must appear in its cited chunk, else re-map or flag), or RAGAS faithfulness in
Phase 7. Recommendation: add the numeric support check early in Phase 4, where SQL answers make figures
even more central.

## D-033 Phase 4 answers (accepted, 2026-11-07, user approved)
1. Policies are enforced by rewriting each validated query: every mart table becomes a subquery that applies
   the caller's row filter (OR over their groups; deny by default) and replaces masked columns with masked
   values, so predicates/joins/aggregates only see masked values (Ranger-view semantics).
2. Users see the generated SQL in the `[Tn]` citation; the policy-rewritten SQL that ran is in the audit.
3. SQL functions are allowlisted (anything else, incl. table functions, file readers, getenv, system
   functions, is refused). Defence in depth: the DuckDB instance is read-only with external access,
   extension autoload and community extensions off, configuration locked, memory/threads capped, and a
   query timeout (connection interrupt).
4. Figure support: re-map to the best supporting source, else refuse (blocked_reason unsupported_figure).
5. A user without table grants is never routed to data (no LLM routing call either); a keyword heuristic
   only adds a neutral note, which never names tables.

## D-034 DuckDB hardening is per database instance (accepted, 2026-11-07)
DuckDB shares one database instance per file per process, and `lock_configuration` locks that instance.
The engine therefore hardens once and verifies the settings on every later engine (refusing to run if the
instance is not hardened); queries use cursors on the hardened connection. Policy decisions travel with
their result (`execute_with_decision`) so concurrent requests cannot swap audit details.

## D-035 Figure support is segment-level, with a list fallback (accepted, 2026-11-07)
Number presence alone passes the r2 case (the wrong document also holds "40%", in another row), so support
is judged on the segment holding the figure and its overlap with the sentence's terms. A sentence listing
several result rows may be supported figure-by-figure across rows of the same source (scored lower).
Numbers carry no sign, so ids like `R-00001` are not read as -1. Effect: Phase 3 golden citation
correctness 0.957 -> 1.0 with answer correctness unchanged (docs/eval/golden-phase4-support.json).

## D-036 Router hands text-to-SQL only the data part of a combined question (accepted, 2026-11-07)
Found in Phase 4: given the whole combined question, the model invented a `loan.ltv_pct` column to cover the
policy half. The router now returns `data_question` (standalone, data part only); text-to-SQL receives that.
A `not_granted` refusal is never fed back to the model (no coaching towards tables the user may not see).

## D-037 Known limit: masked-value questions get "not covered" (open, Phase 6 UX)
Asking for names/emails as a masked user returns "not covered" rather than "these values are masked for
you". Safe (nothing leaks); a clearer message is a UI/answer-template improvement for Phase 6.

## D-038 FIBO lives in `.infra/ontology/fibo`, not `data/ontology/fibo` (accepted, 2026-11-07)
The skill says `data/ontology/fibo/<version>/`. FIBO is pinned infrastructure (release master_2026Q3,
sha256 46385076…0694), downloaded once like the other binaries, and must survive `data/` resets. Only the
modules FND, BE, FBC, LOAN, SEC are indexed (1,881 classes). FIBO 2026Q3 moved Organization, Party,
Document and similar to OMG Commons (`cmns-*`), which we do not load; local concepts hang off the closest
FIBO class that exists (D-043).

## D-039 graphify runs isolated, on a local model, behind a network guard (accepted, 2026-11-07)
`graphifyy[ollama,office]==0.9.95` lives in its own uv env (`infra/local/graphify`), never in the app env.
It is invoked as a subprocess with `kb.envsafe` (no credential variables), `--backend ollama`, a loopback-only
`OLLAMA_BASE_URL` (non-loopback refused before launch), and a `sitecustomize` guard that blocks and logs any
non-loopback socket; a non-empty guard log fails the build. A timeout kills the whole process tree
(`graphify.exe` is a launcher; killing it alone orphaned the python worker and kept Ollama busy).

## D-040 graphify LLM settings: pinned context and output, Qwen3 sampling, small parts (accepted, 2026-11-07)
Measured on retail: graphify sizes `num_ctx` per request (~20k), which reloaded qwen3:8b on every call,
spilled 2/37 layers off the 8 GB GPU (22 tok/s), and its 16,384-token output cap let repetition loops run
into Ollama's 10-min wall (first build timed out after 1 h). Greedy decoding (temperature 0) made qwen3
loop on long JSON ("... and Documentation and Documentation ..."), as Qwen documents. Settings now:
`qwen3-graphify` (thinking off; top_p 0.8, top_k 20, presence_penalty 1.5), temperature 0.7 with seed 42,
`num_ctx` 12288, output cap 3072, each document exported as ~2,000-char parts (heading/paragraph
boundaries, tables never cut) with token budget 400 so each part is one LLM call. Retail: 28 parts,
7 hit the cap (partial result kept), 2 gave no JSON after 3 tries (re-extracted next run), 29 min.
Labels from repetition loops are dropped by `degenerate()` and counted in the report.

## D-041 Every graphify node is an entity (bug fix, 2026-11-07)
`file_type` in graphify's schema is the node's source category (code|document|paper|image|rationale|
concept), not "this node is the file". The first normaliser treated `file_type == document` as the
document itself and dropped 58 of 70 retail nodes (coverage 0.08). All nodes are now entities.

## D-042 Embedding matches are never auto-accepted (accepted, user decision 2026-11-07)
At 0.85, embedding matches auto-accepted wrong concepts ("Records" -> PublicRecord 0.86). Accepted concepts
steer expansion, so only rule matches (exact label 1.0, synonym 0.95, head term 0.88) auto-accept;
every embedding match goes to the checker queue with its best candidate and cosine.

## D-043 Curated bank-governance vocabulary in `config/ontology.yaml` (accepted, user decision 2026-11-07)
Retail after D-041/D-042: 22/56 concept entities auto-mapped (39%) vs the skill's >= 80%. Many entities
have no FIBO class (committees, procedures, roles, escalation, thresholds). The user chose: curate generic
bank vocabulary as reviewed synonyms or `kb:` local concepts, report the real coverage, and leave the
rest to checkers (Claude makes no checker decisions). Added: GovernanceCommittee, OperatingProcedure,
ProductDisclosureDocument, ControlRole, Escalation, ApprovalThreshold, ControlBreach, SystemOfRecord;
synonyms for Regulation, Duty, CreditRiskAssessment; more PolicyException labels. Terms are generic (no
entity-specific labels: "Tier A".."Tier D" stay in review). Retail: 30/60 auto-mapped (0.50), 30 queued.
All four BUs (run 20261107-205706 + retail re-normalise): 87/230 auto-mapped (0.38); 143 queued.
The `>= 80%` DoD item stays failing in `test_mapping_coverage_at_least_80_percent` until checkers act.

## D-044 Reranker scores expansion hits against the question + spotted concept labels (accepted)
Expansion found the right chunk for "What does a wire transfer cost?" (concept TelegraphicTransferFee,
BM25 18.6, rank 1 in the concept branch), but bge-reranker-base scored it 0.003 against the user's
wording, below `min_rerank_score` 0.05, so it was dropped; scored against the question plus the label
"telegraphic transfer fee" it gets 0.51. The rerank query now appends the preferred labels of concepts
spotted in the question. Only ontology labels of concepts in the question are added (no related
concepts, no BU graph content), so nothing outside the caller's scope reaches the query; candidates are
still the pre-filtered ones. Unit test: `tests/unit/test_expansion_rerank.py`.

## D-045 Phase 6 scope decisions (accepted, user decision 2026-11-08)
Recommended options approved: (1) Office files in the inspector show Docling structure (blocks, chunks)
without a page image (no LibreOffice); (2) makers re-parse; exclude is single-actor (restricts access);
re-include is a four-eyes INCLUDE_DOCUMENT request; (3) answer flags go to makers/checkers of the BUs whose
documents were cited, never with the question text (that stays auditor-only in `kb.query_log`);
(4) the UI uses the existing local JWT login only; the X-KB-User header stays test-only.

## D-046 Docling 2.142 parser options (API differs from older docs)
`force_full_page_ocr` is deprecated; full-page OCR is `RapidOcrOptions(mode=OcrMode.FULL_PAGE)`.
Table options are `TableStructureOptions(mode=TableFormerMode.ACCURATE|FAST, do_cell_matching=bool)`.
Re-parse options are allow-listed twice: in `kb.request_reparse` (DB) and `DoclingParser.option_key`.
One converter is cached per option set.

## D-047 Re-parse = version n+1 with its own raw copy (accepted)
A re-parse copies the current version's raw object to a new key (checksum verified) and indexes it as
version n+1 through the normal path (`_flip_current`), so the old version is SUPERSEDED with retention and
can be purged without touching the new one. cTag is copied (delta sync sees no change); a checker-approved
PII override carries over (same sha256). If the new version fails, the old one stays current and the
request is FAILED. Queued by the gateway, executed only by the pipeline (Dagster `reparse_sensor`).

## D-048 Exclude / include mechanics (accepted)
Exclude: the database decides first (`kb.exclude_document`, recorded as an EXECUTED EXCLUDE_DOCUMENT
request), then the gateway hides the document in the index at once; the index-sync sensor closes the
version. A trigger lets a document leave EXCLUDED only inside the governance functions, so not even the
pipeline role can bring it back; syncs skip excluded documents and `process_version` refuses them before
touching the index. Include: an approved INCLUDE_DOCUMENT sets `restore_pending`; index sync re-stamps the
ACL to today's rule *before* making the latest hidden version visible, and clears the source's delta
token so the next sync (a full, idempotent listing) picks up edits skipped while excluded (found by
`test_inspector.py`).

## D-049 Inspector access authority is the index ACL (accepted)
A caller may open a version (metadata, blocks, chunks, raw file) only if at least one of its chunks passes
their filter: the mandatory retrieval filter for everyone; for MAKER/CHECKER also `build_inspection_filter`
(still their ACL groups, only BUs they govern, but non-current versions included). Otherwise 404, never 403
(no existence leak). Private uploads stay visible only to the uploader.

## D-050 UI dependency pins changed after npm audit (accepted)
The plan's first picks had advisories: react-router-dom 6.x (open redirect; fixed only in >= 7.18.0),
vite <= 6.4.3 (dev-server path traversal, esbuild), @playwright/test < 1.55.1 (browser download without
TLS verification). Pinned react-router-dom 7.18.4, vite 8.3.5, @playwright/test 1.64.1 (all >= 2 weeks old,
`npm audit` 0). React stays 18.3.1. The JWT is kept in sessionStorage (prototype; a strict CSP is set on
every gateway response); for CML, prefer SSO cookies (Phase 7).

## D-051 Migration 008 instead of editing 007
007 reused `kb.tg_audit_append_only()`, whose message names audit_log for every table. 007 was already
applied to the live DB and the runner refuses edited migrations, so 008 adds `kb.tg_append_only()` (same
rule, honest message) instead of bypassing the checksum guard.

## D-052 BU spaces in chat narrow documents and graph expansion only
`/ask` accepts `spaces`; `build_access_filter` adds a BU term on top of the unchanged ACL clause, so a space
can only narrow (naming a BU you cannot read returns nothing). Structured data stays governed by the
PolicyEngine grants alone.

## D-053 Browser acceptance runs on the live stack with run-unique fixtures
Playwright drives the real gateway (serving `ui/dist`), Dagster sensors and Ollama. `synthetic/e2e.py`
creates facts unique to each run (a SharePoint folder `retail/E2E/<run>/` outside every live source's
`/KB` scope, and an upload PDF); `afterAll` excludes / deletes through the gateway and deletes the run's
drive files, so nothing stays searchable. Each run leaves one ACTIVE source `ret-e2e-<run>` with no live
documents in the registry (audit trail kept by design).

## D-054 Line endings pinned to LF
Patch scripts that used `Path.write_text` on Windows rewrote 26 tracked files with CRLF (content
unchanged). Normalised in one commit and pinned with `.gitattributes` (`* text=auto eol=lf`).

## D-055 Open: e4 flips to a refusal with expansion on (answer quality, Phase 7)
Phase 6 regression runs: documents golden 1.0 on every metric; structured 0.929 once (d10: text-to-SQL
joined a non-existent `loan.ltv_pct`), 1.0 on two reruns, so LLM variance. Expansion e4 failed with
expansion on in 3 of 3 runs this session (it passed in the Phase 5 run): with expansion the reranker
puts the shared "Table 1: Maximum loan-to-value" chunk first (scores 0.990 / 0.985 / 0.984, a near tie),
and qwen3:8b cites it as `[T1]`, copying the table caption; `[T1]` is the data-citation form, there is no
data source, so the citation guard refuses the answer. Not caused by Phase 6 code (no retrieval or answer
change when `spaces` is unset). Not "fixed" by remapping `[T<n>]` to a document, which would weaken the
guard. Phase 7 candidates: a data-citation label that cannot collide with captions, or caption-aware
context rendering. Expansion still improves e7 (0.875 -> 1.0 on hit@5; answer_correct net 0.875 both ways).

## D-055 update (Phase 7, 2026-11-08): refusal fixed; the remaining e4 miss is a corpus conflict
- Data citations are now `[SQL<n>]`, a prefix no caption uses. The prompt shows only the ref kinds actually
  supplied, so a documents-only prompt never mentions a data ref. A rule says captions such as "Table 1"
  are never refs. Any letter-prefixed ref the model invents (e.g. `[T1]`) is still parsed, recorded as
  invalid and stripped; the guard is unchanged (tests: `test_caption_style_ref_is_not_a_data_citation_d055`,
  `test_prompt_shows_only_supplied_ref_kinds_and_the_caption_rule`).
- Live rerun: e4 with expansion is no longer refused; it answers "45% [1]", grounded and correctly cited.
  The expected fact is 35%. Cause: two documents the corporate user may read disagree. The shared
  *Group Credit Risk Appetite Statement* gives land for development, first facility, 45%; the corporate
  *Commercial Property Lending Policy* gives 35%. Expansion lifts the group document to rank 1 (0.990 vs
  0.984) and qwen3:8b answers from [1] only. A prompt rule asking the model to report each conflicting
  value was tried and made no difference, so it was reverted rather than kept untested.
- Open (user decision): source precedence when a BU policy and a group policy disagree.
  Documents golden 1.0 on all metrics; structured citations_ok 1.0 (data cites are `[SQL1]`); expansion
  hit@5 1.0 on, answer_correct 0.875 both ways, e7 improved, e4 as above.

## D-056 Tracing: OTel SDK to Phoenix, linked to the audit log, no text in spans (Phase 7)
- `local.PhoenixTracer` uses opentelemetry-sdk 1.45.1 with a private TracerProvider (not the global
  one) and a BatchSpanProcessor exporting OTLP/HTTP to `http://127.0.0.1:6006/v1/traces`, project `kb`
  (resource attribute `openinference.project.name`). It refuses a non-loopback endpoint. `cml.OtelTracer`
  is the same export to an in-cluster collector and requires https. `local.NullTracer` is used by offline
  evaluation and unit tests.
- The Tracer port now yields a span handle (`trace_id`, `set()`, `error()`) instead of a bare string,
  because the audit id is only known after the span opens.
- Spans per question: `kb.ask` (root) > `kb.route`, `kb.sql`, `kb.retrieve`, `kb.answer`. The QUERY audit
  row stores `trace_id`; the root span stores `kb.audit_id` and `kb.query_id`.
- Spans carry ids, counts, flags and the question's SHA-256, never question or answer text. The query log
  stays the single access-controlled copy of what was asked. Test:
  `test_spans_carry_no_question_or_answer_text`.
- Verified live: audit 179 -> trace 78a132124328bf0d7f32e28a274743ea in Phoenix; that trace's `kb.ask`
  span carries kb.audit_id 179.

## D-057 RAGAS in an isolated, network-guarded env with a second local judge (Phase 7)
- RAGAS 0.4.3 (verified: `ragas.metrics.collections` Faithfulness / AnswerRelevancy / ContextPrecision /
  ContextRecall with `ascore(...)`; `llm_factory(model, provider="openai", client=...)`) brings langchain,
  langsmith, instructor and the openai client, and sends usage telemetry to t.explodinggradients.com unless
  `RAGAS_DO_NOT_TRACK=true`. It therefore runs only in `infra/local/ragas/.venv` as a subprocess
  (`kb.evaluation.ragas_runner` -> `infra/local/ragas/score.py`) with: the kb.envsafe sanitized environment
  (which already strips every LANGCHAIN_/LANGSMITH_ variable, so LangSmith tracing cannot be switched on), the
  telemetry opt-out, HF offline, and the existing network guard (sitecustomize) on PYTHONPATH. Any recorded
  outbound attempt fails the run even though it was blocked. The scorer itself refuses to start without the
  guard, the opt-out, a loopback judge or a local embedder directory.
- ragas 0.4.3 imports `langchain_community.chat_models.vertexai`, removed in langchain-community 0.4, so the
  env pins `langchain-community<0.4` (resolved 0.3.31).
- Judge: `llama3.1:8b` (a second local model), so qwen3:8b does not grade its own answers. Embeddings for
  answer relevancy: the same pinned bge-small, loaded from its local path.
- Security trade-off, flagged to the user: `score.py` imports the openai client (pointed at Ollama on
  loopback). `test_no_cloud_sdk_imports` allows exactly that one file, and a companion test fails if the file
  imports anything else from the cloud-SDK list or loses any of its refusals.
  `tests/security/test_ragas_isolation.py` proves an outbound call from the RAGAS env is blocked and logged.
  Alternative if the user prefers: our own judge wrapper over httpx, no openai package anywhere.
- Smoke test: a faithful answer scored faithfulness 1.0, a fabricated one 0.0; 0 outbound attempts.

## D-058 Operations dashboard: a UI page behind GRP_KB_OPS, counts only (Phase 7)
- User-approved recommendations: dashboards are a page in the existing UI (no Grafana), visible to a new
  `GRP_KB_OPS` group with one synthetic user `ops.analyst`.
- `/api/v1/ops/ingestion|exceptions|eval|feedback` return registry counts and metadata only: source names,
  run states, exception reasons and ages, metric values, flag counts. No document text, question text or chunk
  ids (test `test_ops_payloads_carry_no_question_text_or_chunk_ids`). `GRP_KB_OPS` is kind `ops`: it grants no
  readable BU and no governance role, so the dashboard is not a side door to content.
- The leak suite iterates every user in `config/users.yaml`, so `ops.analyst` is now covered by it (it must
  see nothing).
- Chart: one line per metric (answer correct, faithfulness, context recall) on a 0-1 axis, categorical slots
  1-3 of the validated palette, direct end labels de-collided, legend, and the runs table as the table view.

## D-059 Ontology star map: FIBO is public, the BU overlay is not (Phase 7)
- `python -m kb.graph.starmap` builds `data/ontology/starmap/fibo-<release>.json` from the pinned FIBO release
  (all 11 modules: 277 ontologies, 2,768 classes, 1,190 properties) plus our 25 local extension concepts linked
  to their broader FIBO class. FIBO individuals (~11.5k code-list entries) are left out. The layout is
  structural and deterministic (modules on a sphere, ontologies around modules, members around ontologies):
  networkx's force layout took over 10 minutes on ~5k nodes and was replaced. The rebuild is byte-identical
  (test).
- `/api/v1/ontology/fibo` serves it to any signed-in user (no bank data). BU entities come only from
  `/api/v1/ontology/overlay`, read from the named graphs of the caller's readable BUs (same scoping as graph
  expansion) and re-checked row by row. `/entity` resolves source documents through the chunk index with the
  caller's access filter, and mart hints through `SqlEngine.permitted_schema` (works with Starburst on CML).
  Entity IRIs must match the `entity_iri()` form before they reach SPARQL.
- Colour by job, not by module: FIBO is neutral context; local concepts and a picked module are highlighted;
  BU entities wear status colours (mapped / pending review) with text labels. Twelve module hues would break
  the categorical limit for a scatter-like view, so the module is a filter instead.
- three.js 0.171.0 is bundled (the strict CSP forbids CDNs).

## D-060 Evaluation findings from the first full `kb.ps1 eval` (Phase 7)
- PASS: golden answer_correct 0.96, hit@5 0.96, abstain 1.0; structured data_correct 1.0; RAGAS faithfulness
  0.90, context recall 1.0 (30 answers, judge llama3.1:8b, 0 outbound attempts); leak suite 40 tests, 0 leaks.
- The run asks every question through `AskService.compute`, i.e. through the router, as users do. This found
  something the Phase 3-5 suites (which call the retriever directly) could not: the router sends two
  document questions to text-to-SQL. e8 "lending ratio for an office building on a third facility" ->
  "0.56 [SQL1]"; w5 "how much cash does the Growth model portfolio hold" -> a sum over portfolios. Both
  answers are grounded in a real query result, so no guard fired; they answer a different question.
  Not fixed in Phase 7: a router change needs its own golden runs. Recorded as open.
- e4 remains the group-vs-BU policy conflict (D-055 update).
