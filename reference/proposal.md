# Enterprise Knowledge Base: architecture proposal and roadmap

**Provenance:** authored by Claude on 2026-11-07 at the user's request (the original was not
supplied). It consolidates `CLAUDE.md` (the brief) and the earlier POC material in
`../ekb/` (EKB_POC_Build_Spec.md, Enterprise_Knowledge_Base_Framework.docx). Where this file and
`CLAUDE.md` disagree, `CLAUDE.md` wins.

## 1. Problem

Group Data Office staff look for answers across SharePoint policy libraries, product sheets,
procedures and Hadoop data marts. Today that means knowing which site, which file and which
mart to look in, and access rules are enforced differently in each place. The goal is one
question box that answers in natural language, cites its sources, and never shows a user
anything their access groups do not allow.

## 2. Scope

**In scope (prototype -> CML pilot)**

- Unstructured: SharePoint document libraries onboarded per BU; user uploads to approved BU spaces.
- Structured: four synthetic marts locally; Starburst-fronted Hadoop marts on CML.
- Governance: maker-checker onboarding of every source and every publish of an upload; legal
  hold; retention purge with certificates; full audit.
- Answering: hybrid retrieval with rerank, text-to-SQL under Ranger policies, a router that
  combines both, ontology-assisted expansion (FIBO) per BU.
- Evaluation: per-BU golden sets, ACL leak suite, RAGAS, tracing.

**Out of scope (for now)**

- Write-back to SharePoint or the marts; agents that take actions.
- Cross-BU answer synthesis beyond the `shared` group-policy area.
- Real customer data anywhere outside the bank's CML tenancy.
- Hosted LLM or embedding APIs, at any stage.

## 3. Principles

1. **One door.** The gateway is the only path to Elasticsearch, the SQL engine and the graph.
2. **Pre-filter, never post-filter.** ACL, currency and source-active filters live inside the
   kNN and BM25 queries; SQL and graph traversal run as the end user.
3. **Four-eyes in the database.** Constraints and triggers make maker = checker impossible,
   even for a buggy or compromised API.
4. **Ports and adapters.** Local and CML differ only by adapters chosen in configuration.
5. **Idempotent and auditable.** Deterministic ids, append-only audit, delta tokens committed
   after success, reruns converge.

## 4. Architecture

```
 UI (React)  ──►  Gateway (FastAPI)  ──►  Agent router ──► Retrieval (Haystack: BM25+kNN ► RRF ► rerank ► LLM)
                    │ auth, groups,                  └──► Text-to-SQL (sqlglot ► PolicyEngine/Ranger ► engine)
                    │ mandatory filters,             └──► Graph expansion (named graph per BU)
                    │ audit
                    ▼
               Registry (PostgreSQL: sources, revisions, approvals, documents, audit)
                    ▲
 Ingestion (Dagster): DocumentSource ► gates (type, size, malware, PII) ► RawStore ► Docling parse
                      ► HybridChunker ► Presidio ► Embedder ► ChunkIndex (ES) ► version flip
```

### Ports

| Port | Local | CML |
|---|---|---|
| DocumentSource | LocalFolderSource (Graph-delta emulation), UploadSource | SharePointGraphSource, UploadSource |
| MalwareScanner | AllowListScanner (type checks only, logs "not scanned") | bank AV service |
| RawStore | fsspec local | fsspec HDFS / Ozone |
| Registry | PostgreSQL 17 | bank PostgreSQL |
| ChunkIndex | Elasticsearch 8.19 | Elasticsearch cluster |
| Embedder | bge-small-en-v1.5 (384-d) | BGE-M3 on Ray Serve (1024-d) |
| Reranker | bge-reranker-base | bge-reranker-v2-m3 on Ray Serve |
| LLM | Ollama (OpenAI-compatible) | vLLM (OpenAI-compatible) |
| SqlEngine | DuckDB + PolicyEngine | Starburst with impersonation, Ranger |
| IdentityProvider | users.yaml | SSO + LDAP groups |
| GraphStore | pyoxigraph, named graph per BU | same or governed store |
| Tracer | Phoenix | Phoenix / OTel collector |

### Data model essentials

- `kb_source` + immutable `source_revision` (ACL groups, classification, retention, file rules).
- `approval_request` with DB-enforced four-eyes; request types grow per phase
  (onboard, change, reactivate, release exception, release legal hold, publish document).
- `document` / `document_version` with exactly one current version; ES `is_current` switched
  before the registry flip so search never shows two versions or none.
- Chunk documents carry `doc_id`, `version_id`, `acl_groups`, `is_current`, `source_active`,
  `bu`, page, section path and `concept_ids`.

## 5. Security model

| Threat | Control |
|---|---|
| User sees another BU's content | ACL pre-filter in both retrievers; leak suite with canaries per BU |
| Private upload leaks to colleagues | `acl_groups = [user:<id>]` until a checker approves publish |
| Self-approval | DB constraint + function guard; pipeline role has no decision grants |
| Prompt injection via documents | retrieved text wrapped as data; system prompt forbids following it; no tools reachable from retrieved text |
| SQL exfiltration | sqlglot: SELECT only, row limit, no cross joins on large tables; masks and row filters applied by rewrite (local) or Ranger (CML) |
| Hosted-model leakage | no hosted endpoints configured; subprocess env sanitized; test asserts it |
| Malicious upload | magic-byte/type agreement, size, AV scan (CML), PII gate, exception queue |

## 6. Roadmap

| Phase | Outcome | Exit criterion |
|---|---|---|
| 0 Foundations | scaffold, infra scripts, ports, config, synthetic world | up + seed + test green; versions pinned |
| 1 Registry | migrations 001/002, maker/checker API | DB-level four-eyes proven by tests |
| 2 Ingestion | Dagster pipeline behind ports, uploads, lifecycle | scripted lifecycle + upload scenarios pass |
| 3 Doc Q&A | gateway, hybrid retrieval, cited answers | zero canary leaks; golden baseline recorded |
| 4 Structured | PolicyEngine, text-to-SQL, router | masks/row filters hold; DML/DDL rejected |
| 5 Ontology | graphify -> FIBO, SHACL, per-BU graph | no cross-BU expansion; one golden question improves |
| 6 UI | chat, uploads, onboarding, checker inbox, inspector | both end-to-end loops work in the browser |
| 7 Eval & ops | RAGAS, dashboards, CML deployment guide | `make eval` report; guide covers every port |

## 7. Risks and open points

1. **Local model quality.** An 8 GB laptop GPU limits the LLM to ~7B; table arithmetic was weak
   in the earlier POC. Mitigation: compute numeric answers in SQL, not in the LLM.
2. **Embedding dimension differs** between local (384) and CML (1024): indexes are rebuilt, not
   migrated. Recorded as accepted.
3. **graphify uses an LLM for documents.** It must be pointed at the local endpoint only, with
   API-key variables stripped from its environment.
4. **Ranger emulation drift.** The local PolicyEngine is an approximation; Phase 4 tests must be
   rerun against Starburst + Ranger on CML before go-live.
5. **Scanned PDFs** need OCR model weights; they must be pinned and available offline.
