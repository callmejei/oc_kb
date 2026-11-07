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
