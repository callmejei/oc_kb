---
name: graphify-fibo
description: Build the per-BU ontology layer for the knowledge base. Runs graphify on each BU's parsed corpus with a local LLM only, maps extracted entities to FIBO concepts, validates with SHACL, loads one named graph per BU into pyoxigraph, routes low-confidence mappings to a review queue, writes concept IRIs onto ES chunks and maintains the concept-to-mart table_map. Use in Phase 5 or whenever the graph layer is rebuilt.
---

# graphify -> FIBO mapping layer

**Provenance:** authored by Claude on 2026-11-07 at the user's request (the original was not
supplied). Verify every graphify CLI flag and API against the installed version before use
(`graphify --help`, read site-packages); record differences in `DECISIONS.md`.

## Inputs

- Parsed Docling output per document version (Markdown + JSON) from the raw/processed store,
  current versions only, grouped by BU (`retail`, `corporate`, `wealth`, `shared`).
- FIBO: pinned release, downloaded once into `data/ontology/fibo/<version>/` (offline afterwards).
  Use only the modules needed: FND (Foundations), BE (Business Entities), FBC (Financial
  Business and Commerce), LOAN, SEC (Securities) as needed by the corpus.
- `config/ontology.yaml`: label synonyms, allowed FIBO classes per BU, confidence thresholds.

## Hard rules

1. **Local LLM only.** Install `graphifyy` with the `ollama` extra only. Run graphify through
   `kb.envsafe.sanitized_env()` so no `*_API_KEY`, `OPENAI_*`, `ANTHROPIC_*`, `GEMINI_*`,
   `AWS_*`, `AZURE_*` variable reaches it; set the base URL to the local Ollama endpoint. A test
   must prove the subprocess environment holds no such key.
2. **One BU per run.** graphify never sees two BUs' documents in one invocation; output graphs
   are written to `urn:kb:graph:<bu>` and nowhere else.
3. **Provenance on every triple.** Each entity node carries `kb:fromChunk` (chunk id),
   `kb:fromVersion`, `kb:bu`, and graphify's edge tag (`EXTRACTED` / `INFERRED`).
4. **No auto-accept below threshold.** Mappings with confidence < `auto_accept` (default 0.85)
   go to the review queue; nothing in the queue is used for expansion until a checker accepts it.

## Steps

1. **Extract.** For each BU, export current-version Markdown to a temp folder and run graphify
   on it (semantic pass via local LLM). Keep graphify's raw JSON graph as an artefact
   (`data/ontology/runs/<run_id>/<bu>/graph.json`).
2. **Normalise.** Convert graphify nodes/edges to RDF with rdflib under
   `https://kb.local/id/<bu>/` IRIs (deterministic: hash of BU + normalised label).
3. **Map to FIBO.** For each entity: exact label/altLabel match -> synonym table -> embedding
   similarity (local Embedder) against FIBO labels restricted to the BU's allowed classes.
   Emit `skos:exactMatch` (>= auto_accept) or `skos:closeMatch` + review item (below).
4. **Validate.** Run pySHACL with `shapes/kb-shapes.ttl`: every mapped entity has exactly one
   `kb:bu`, at least one `kb:fromChunk`, a FIBO class within the BU's allowed set, and no edge
   whose endpoints have different `kb:bu`. Violations block the load.
5. **Load.** Replace the BU's named graph in pyoxigraph atomically (load into
   `urn:kb:graph:<bu>:staging`, validate, then swap).
6. **Review queue.** Persist pending mappings (registry table added in Phase 5 migration) with
   the source chunk text, proposed FIBO IRI and confidence; checker accept/reject is audited.
7. **Write back.** Update ES chunks' `concept_ids` with accepted concept IRIs (partial update,
   no re-embedding). Graph expansion at query time reads only named graphs whose BU is in the
   caller's allowed BUs.
8. **table_map.** Maintain `config/table_map.yaml`: FIBO concept IRI -> mart table/columns
   (e.g. `fibo-loan:LoanToValueRatio` -> `mart_credit.collateral.ltv_pct`) so text-to-SQL gets
   concept-aware schema hints, filtered by the caller's table grants.

## Definition of done

- [ ] graphify runs per BU with a sanitized env; test asserts no API-key variables and a local base URL.
- [ ] Each BU has a named graph; a SPARQL test proves zero triples whose `kb:bu` differs from the graph's BU.
- [ ] SHACL validation passes on all loaded graphs; a deliberately broken fixture fails it.
- [ ] >= 80% of entities in the synthetic corpus map to a FIBO class (exact or accepted close match); the rest are in the review queue.
- [ ] Chunks carry `concept_ids`; graph expansion for a user of BU X never returns concepts or chunks from BU Y (security test over every user).
- [ ] `table_map.yaml` covers every mart table, and text-to-SQL uses it only for permitted tables.
- [ ] At least one golden question scores measurably better with expansion than without (report saved).
- [ ] FIBO release, graphify version and thresholds recorded in `VERSIONS.md`.
