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
