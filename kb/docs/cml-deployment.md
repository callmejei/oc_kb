# Deploying the knowledge base to Cloudera Machine Learning (CML)

The CML deployment runs **the same code** as the laptop prototype. Only the adapters behind the 13 ports
change, selected by `config/cml.yaml` (start from `config/cml.example.yaml`). Nothing in `src/kb/` branches
on the environment except adapter selection and config validation.

**Honest status (2026-11-08):**
- **Implemented and tested locally:** every local adapter. Six of them are reused unchanged on CML with different
  config (`local.UploadSource`, `local.FsspecRawStore`, `local.PostgresRegistry`, `local.ElasticsearchChunkIndex`,
  `local.OpenAICompatibleLLM`, `local.OxigraphGraphStore`); they are untested against the bank's services.
- **Implemented, unit-tested only:** `cml.OtelTracer` (the same exporter as the local Phoenix tracer, https
  required).
- **Stubbed:** `cml.SharePointGraphSource`, `cml.BankAvScanner`, `cml.RayServeEmbedder`, `cml.RayServeReranker`,
  `cml.StarburstSqlEngine`, `cml.LdapIdentityProvider`. A stub raises `NotImplementedYetError` on first use; it never
  degrades silently. Each section below says what the real adapter must do and which existing test proves it.
- Nothing here has been run on a CML cluster. Names of bank hosts are placeholders (`*.example.internal`).

## 1. Topology

| Component | Runs as | Notes |
|---|---|---|
| Gateway + UI (FastAPI serving `ui/dist`) | **CML Application** | The only user-facing door. CML SSO in front (`gateway.auth: sso`); the dev header is refused outside `env=local`. |
| Dagster webserver + daemon (sensors, schedules, jobs) | **CML Application** (internal, not shared) | Sensors need a long-running daemon. Dagster storage on the bank PostgreSQL (separate database from the registry). The Dagster UI is for operators only. |
| `make eval` (golden per BU, RAGAS, leak suite) | **CML Job**, nightly and on every release | Connects to the registry as the pipeline role and records `kb.eval_run`. |
| Ontology rebuild (`kb.ps1 ontology`) | **CML Job**, on demand / weekly | Runs graphify per BU in its isolated env. |
| Embedder, reranker | **Ray Serve** on CML workers | See section 4. |
| LLM (answering) and RAGAS judge | **vLLM** (OpenAI-compatible) | See section 4. |
| Registry, chunk index, SQL engine, AV, LDAP, tracing | **Bank services** | PostgreSQL, Elasticsearch, Starburst + Ranger, AV scanner, AD/LDAP, Phoenix or an OTel collector. |

The container image for the Application and the Jobs is in section 9.

## 2. Ports: the adapter swap

| Port | Local adapter | CML adapter | Status |
|---|---|---|---|
| `document_source` | `local.LocalFolderSource` | `cml.SharePointGraphSource` | stub |
| `upload_source` | `local.UploadSource` | `local.UploadSource` (root on HDFS/Ozone) | implemented |
| `malware_scanner` | `local.AllowListScanner` | `cml.BankAvScanner` | stub |
| `raw_store` | `local.FsspecRawStore` (`file://`) | `local.FsspecRawStore` (`hdfs://` or `ofs://`) | implemented |
| `registry` | `local.PostgresRegistry` | `local.PostgresRegistry` (bank PostgreSQL) | implemented |
| `chunk_index` | `local.ElasticsearchChunkIndex` | `local.ElasticsearchChunkIndex` (bank cluster) | implemented |
| `embedder` | `local.SentenceTransformerEmbedder` | `cml.RayServeEmbedder` | stub |
| `reranker` | `local.CrossEncoderReranker` | `cml.RayServeReranker` | stub |
| `llm` | `local.OpenAICompatibleLLM` (Ollama) | `local.OpenAICompatibleLLM` (vLLM) | implemented |
| `sql_engine` | `local.DuckDbPolicySqlEngine` | `cml.StarburstSqlEngine` | stub |
| `identity_provider` | `local.YamlIdentityProvider` | `cml.LdapIdentityProvider` | stub |
| `graph_store` | `local.OxigraphGraphStore` | `local.OxigraphGraphStore` (persistent volume) | implemented |
| `tracer` | `local.PhoenixTracer` | `cml.OtelTracer` | implemented, unit-tested |

Config validation (`kb.config`) runs at start-up on CML exactly as locally:
- Every port must be configured.
- Every host must be in `network.allowed_hosts`.
- A model port pointing at a hosted AI API (OpenAI, Azure OpenAI, Anthropic, Gemini, OpenRouter, …) is rejected
  even if someone allow-lists it.

### document_source

- **CML adapter:** `cml.SharePointGraphSource` (msal + Microsoft Graph delta).
- **Must reproduce what `LocalFolderSource` simulates:**
  - stable `driveItem` ids;
  - cTag-based change detection;
  - the `deleted` facet;
  - moves out of the configured folder treated as deletes;
  - `410 Gone` / expired delta token → full resync;
  - delta tokens committed only after a successful run (the engine already does this).
- **Auth:** app registration with `Sites.Selected` (per-site grant, not tenant-wide `Sites.Read.All`). Prefer a
  certificate credential over a client secret.
- **Config:** `tenant_id`, `client_id`, and the credential from the CML secret store. Hosts `graph.microsoft.com` and
  `login.microsoftonline.com` are the only external ones in `allowed_hosts`.
- **Prove it:** run `tests/integration/pipeline/test_lifecycle.py` and the delta semantics tests against a test site
  with the adapter bound.

### upload_source

- **CML adapter:** the same `UploadSource`.
- **Storage:** uploads stream through the gateway into the raw store (never held whole in memory), so on CML the
  root is an HDFS/Ozone path.
- **Gates (unchanged):** type, size, malware (the `malware_scanner` port), PII.
- **Prove it:** `tests/integration/pipeline/test_uploads.py`.

### malware_scanner

- **CML adapter:** `cml.BankAvScanner`, which calls the bank's existing AV scanning service.
- **Fail closed:** a timeout, an unknown verdict or an unreachable service is an exception-queue item, never a pass.
  The local `AllowListScanner` logs "not scanned"; the CML adapter must return a real verdict.
- **Config:** `url`, a service credential, TLS verification on.

### raw_store

- **CML adapter:** the same `FsspecRawStore`, with `url: hdfs:///data/kb/raw` (or `ofs://` for Ozone).
- **Dependency:** needs the fsspec HDFS driver (`pyarrow` HDFS) in the runtime image and a Kerberos ticket for the
  service principal (keytab mounted from the secret store).
- **Retention:** retention dates and legal hold stay in the registry; purge certificates are written by the pipeline
  as locally.

### registry

- **CML adapter:** the same `PostgresRegistry`, against the bank PostgreSQL.
- **Migrations:** run `python -m kb.registry.migrate` as the owner role. The runner checksums every migration and
  refuses edited ones; all four-eyes rules live in the database (triggers, constraints, `SECURITY DEFINER`
  functions).
- **Login roles:** `kb_api` (gateway, member of `kb_app`) and `kb_pipe` (Dagster and Jobs, member of
  `kb_pipeline`). The pipeline role can never decide approvals; the API role can never record evaluation runs.
- **DSN:** `sslmode=verify-full`.
- **Prove it:** `tests/integration/registry/` against a scratch database on the bank cluster.

### chunk_index

- **CML adapter:** the same `ElasticsearchChunkIndex` against the bank cluster.
- **ACL filters** (`acl_groups`, `is_current`, `source_active`) are built only in `kb.gateway.acl` and go inside both
  the kNN and BM25 queries (pre-filter).
- **Re-embed, don't migrate:** the CML embedder is BGE-M3 (1024 dims) instead of bge-small (384), so the CML index is
  a new index (`dims: 1024`) filled by a first load. Vectors from the laptop are never copied.
- **Auth:** an API key for a role limited to the `kb-chunks*` indices.
- **Prove it:** `tests/integration/qa/test_leak_suite.py` (zero canary leaks for every user) before go-live.

### embedder

- **CML adapter:** `cml.RayServeEmbedder`. It POSTs batches to the Ray Serve embedding deployment (section 4) and
  checks the returned dimension against `dims`.
- **Pin:** `BAAI/bge-m3` with a pinned revision, recorded in `VERSIONS.md` at deployment time.
- **No fallback:** the adapter must not fall back to a local or hosted model.

### reranker

- **CML adapter:** `cml.RayServeReranker`, which scores (query, chunk) pairs with `BAAI/bge-reranker-v2-m3` on Ray
  Serve.
- **Contract:** sigmoid probabilities, so `retrieval.min_rerank_score` keeps its meaning. Re-tune the threshold on
  the golden sets after the switch.

### llm

- **CML adapter:** the same `OpenAICompatibleLLM`, pointed at vLLM (`base_url: https://vllm.example.internal/v1`).
- **Model:** pinned by `KB_LLM_MODEL` and recorded in `VERSIONS.md`.
- **Answer guards are unchanged:** sanitising, delimiters, the sandwich prompt, the guard token, the citation check,
  figure support, and data citations `[SQL<n>]` (D-055).
- **Prove it:** re-run the golden sets per BU (`make eval`), because the model changes.

### sql_engine

- **CML adapter:** `cml.StarburstSqlEngine` (Trino client), executing as the **end user** (see section 5).
- **Locally vs on CML:** locally, `PolicyEngine` rewrites SQL to emulate Ranger. On CML, Ranger enforces table
  grants, row filters and column masks in Starburst, and the adapter does not rewrite. Text-to-SQL validation still
  runs first (sqlglot: SELECT only, row limit, no cross joins on large tables).
- **Schema:** `permitted_schema(identity)` reads `information_schema` as the user, so Ranger hides what they may not
  see.
- **Prove it:** `tests/integration/qa/test_structured.py` (masks, row filters, DML refusal) with real Ranger
  policies mirroring `config/policies.yaml`.

### identity_provider

- **CML adapter:** `cml.LdapIdentityProvider`.
  - The user id comes from the SSO header set by the CML ingress, never from the request body.
  - Groups come from AD over LDAPS (nested groups resolved, as `member_of` is locally).
  - Group names keep the `GRP_KB_<BU>_<READ|MAKER|CHECKER>` convention.
  - `GRP_KB_OPS` gates the operations dashboard.
  - Governance roles sync into `kb.governance_role` (`python -m kb.registry.rolesync`), on a schedule.
- **Prove it:** `tests/integration/registry/test_api_governance.py` and the leak suite with real groups.

### graph_store

- **CML adapter:** the same `OxigraphGraphStore`, on a persistent volume, with one named graph per BU
  (`urn:kb:graph:<bu>`) swapped atomically.
- **Writes and reads:** written only by the ontology Job. The gateway reads it read-only, only for the caller's
  readable BUs.
- **Optional replacement:** a governed graph store can replace it behind the same port. The SPARQL test proving zero
  cross-BU triples must still pass.

### tracer

- **CML adapter:** `cml.OtelTracer`, OTLP/HTTP to the bank's Phoenix or OpenTelemetry collector. It requires https.
- **What spans carry:** ids, counts, flags and hashes only, never question or answer text (D-056).
- **Linking:** the QUERY audit row holds the `trace_id`; the root span holds `kb.audit_id`.

## 3. Secrets

All secrets come from the CML project's environment/secret store, never from `config/cml.yaml` or the image.
`${VAR}` references in the config fail start-up if unset (`load_settings(strict=True)`).

| Variable | Used by | Notes |
|---|---|---|
| `KB_PG_API_PASSWORD` | gateway (`kb_api`) | rotate quarterly |
| `KB_PG_PIPELINE_PASSWORD` | Dagster, Jobs (`kb_pipe`) | |
| `KB_ES_PASSWORD` or an ES API key | chunk index | role limited to `kb-chunks*` |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` (or certificate) | SharePoint source | `Sites.Selected` only |
| Kerberos keytab (mounted file) | raw store (HDFS/Ozone), Starburst | service principal |
| Ray Serve / vLLM bearer token | embedder, reranker, LLM | network-restricted as well |
| Starburst service credential | SQL engine | impersonation rule, section 5 |
| LDAP bind credential | identity provider | read-only bind |
| `KB_LLM_MODEL` | LLM | not a secret; pinned model name |

Subprocesses for graphify and RAGAS are started through `kb.envsafe`, which strips every credential-like variable,
including `*_API_KEY`, `*_SECRET`, `OPENAI_*`, `AWS_*`, `AZURE_*`, `LANGCHAIN_*` and `LANGSMITH_*`. They also run
under the network guard.

## 4. Ray Serve and vLLM endpoints

**Ray Serve (embedder and reranker):**
- **Deployments:** two, on CML workers: `embed` (BGE-M3) and `rerank` (bge-reranker-v2-m3). Revisions are pinned;
  weights are loaded from an internal model mirror.
- **Ingress:** HTTPS ingress, with bearer token auth, reachable only from the gateway Application and the pipeline
  Jobs.
- **Never expose the Ray dashboard.**
  - Start the head with `--dashboard-host=127.0.0.1` (or `--include-dashboard=false`).
  - Publish no CML Application route to port 8265.
  - Add a network policy that drops ingress to 8265 and to the Ray GCS/client ports from outside the Ray pods.
  - The dashboard can submit jobs, so exposing it is equivalent to remote code execution.

**vLLM:**
- **Endpoint:** OpenAI-compatible `/v1`.
- **Models:** the answering model and the RAGAS judge are separate models (D-057), so the judge never grades its own
  answers. Both are pinned.
- **Run flags:** `--disable-log-requests`, so prompts (which contain retrieved text) do not land in vLLM logs, and an
  API key.
- **Thinking off** (`chat_template_kwargs.enable_thinking: false`), as Ollama locally.
- **Capacity:** size `max_model_len` for `retrieval.max_context_chars` plus the answer budget.

## 5. Starburst impersonation and Ranger

- **Service user:** the gateway authenticates to Starburst as a service user (Kerberos or a service credential). Each
  query sets the session user to the **end user's** id (Trino `X-Trino-User` / `session_user`), allowed by a Starburst
  impersonation rule that names only that service user and only the KB user population.
- **Enforcement:** Ranger evaluates every query as the end user: table grants, row filters by BU, column masks. The
  service user itself has **no** data grants, so a query without impersonation returns nothing.
- **Text-to-SQL scope:** text-to-SQL receives only the tables `permitted_schema(identity)` returns. `config/table_map.yaml`
  hints are filtered by those grants, so a concept never leaks the name of a table the user cannot see.
- **Audit:** every statement is audited twice, in the KB audit log (QUERY row: generated SQL, tables, row count) and
  in Ranger's audit (end user, policy decision).
- **Mirror the policies:** `config/policies.yaml` is the local mirror of the Ranger policies. Keep the two in step;
  the structured golden questions (`kind: data` / `both`) are the regression check.

## 6. Network rules

- **Egress:** egress is limited to `network.allowed_hosts`, enforced in config validation and again by the bank
  firewall or CML network policy. There is no general internet egress. Hosted AI APIs are rejected by config
  validation even if allow-listed.
- **Ingress:** only the gateway Application, behind CML SSO. Every question and every UI call goes through it.
- **Internal only:**
  - the Dagster UI;
  - Phoenix (spans hold no question text, but they hold user ids);
  - the Ray Serve and vLLM endpoints.
- **Never exposed:** the Ray dashboard and Ray client/GCS ports (section 4), and the Elasticsearch and PostgreSQL
  admin ports.
- **Guarded tools:** graphify and RAGAS Jobs run under the in-process network guard, which refuses and logs any
  non-loopback connection. On CML, "loopback" becomes the internal vLLM host. Add that host to the guard's allow
  list in the Job image, and keep everything else refused.

## 7. Data migration and first load

1. Apply migrations to the bank PostgreSQL. Run `rolesync` from LDAP.
2. Onboard sources through the maker-checker flow, the same as locally: dry run, approve, first load. No registry
   rows are copied from the laptop.
3. Run a first load per source into the 1024-dimension index.
4. Run the ontology Job per BU. The review queue starts empty for the bank corpus.
5. Run `make eval` with bank golden sets. The synthetic sets in `tests/eval/golden/` stay as the code regression
   suite. Real golden sets are authored by each BU and stored in the bank's environment, never in this repository.

## 8. Evaluation and operations on CML

- **Schedule:** run `make eval` as a nightly CML Job and before each release. A threshold breach or any leak makes
  the Job fail (exit code 1) and is visible on the Operations page (`GRP_KB_OPS`).
- **RAGAS:** runs in its isolated env (`infra/local/ragas`), with the judge on vLLM.
- **Dashboard data:** the Operations page reads only registry tables. No extra metrics infrastructure is needed; if
  the bank wants Grafana, point it at the same read-only views.

## 9. Runtime image

`deploy/cml/Dockerfile` builds the image used by the gateway Application and the Jobs. It is a CML ML Runtime
extended with the locked Python environments and the built UI. It was not built locally: the prototype runs
without Docker by design. Before first use, pick the bank's approved ML Runtime base image and tag, and scan the
image.

## 10. Scale-up option for the data semantic layer: NVIDIA Auto Ontology

At bank scale (hundreds of Hadoop tables), maintaining `config/table_map.yaml` by hand will not keep up. NVIDIA's
open-source Auto Ontology (Apache-2.0, released 2026-10-21) builds certified business terms over relational
schemas, and could author the table map.

**Fit:** offline authoring only, never a runtime dependency:

```
Auto Ontology (offline, local LLM) -> certified terms exported (Ossie YAML)
  -> imported as a maker-checker request (four-eyes, audited)
  -> text-to-SQL schema hints, filtered by the caller's table grants
  -> certified terms mapped to FIBO IRIs, linking them to the document graph
```

**Preconditions before any use:**
1. **Model endpoints:** every role is set to the internal vLLM endpoint. Its default is NVIDIA's hosted API, so a
   test must prove no hosted endpoint is configured.
2. **No query execution:** never use its question-answering or SQL-executing MCP tools. The gateway → Starburst-as-user
   path stays the only way SQL runs.
3. **Profiling:** column profiling and query-history mining run under a masked service identity with sampling off.
   Otherwise sample values and query literals would be copied into its own database, outside Ranger. This needs a
   security sign-off.
4. **Network:** its backend trusts a forwarded identity header and its ingestion endpoints are unauthenticated, so it
   must run on a private network segment.
5. **Pinning:** pin a commit SHA. It has no tagged releases as of 2026-11-08.

## 11. Go-live checklist

- [ ] All six stubbed CML adapters implemented, each with the integration tests named above passing against bank
      services.
- [ ] Leak suite: zero leaks for a representative user from every BU and role, with real AD groups.
- [ ] `make eval` PASS on bank golden sets, judge on vLLM, report recorded.
- [ ] Ray dashboard unreachable from outside the Ray pods (tested with a connection attempt from the gateway pod).
- [ ] Egress test: from the gateway, the Jobs and the RAGAS env, a connection to a public host fails.
- [ ] Secrets only in the CML secret store; `config/cml.yaml` holds `${VAR}` references only.
- [ ] `VERSIONS.md` updated with the CML model revisions (BGE-M3, reranker, vLLM models) and the runtime image digest.
