# kb_pipeline (reference draft)

**Provenance:** authored by Claude on 2026-11-07 at the user's request (the original was
not supplied). **Status:** draft. It has never been run and is not part of `src/kb`. Phase 2
ports it behind `DocumentSource`, `RawStore`, `Registry` and `ChunkIndex`.

| File | Content |
|---|---|
| `kb_pipeline/resources.py` | Graph client (msal client credentials), registry connection, ES client |
| `kb_pipeline/assets.py` | `sharepoint_delta_sync`: delta page loop, item classification (file / deleted / moved out of scope), token committed only after success |
| `kb_pipeline/sensors.py` | `approved_source_sensor`: newly ACTIVE source -> first load |
| `kb_pipeline/jobs.py` | first_load, daily_delta, weekly_reconcile jobs + schedules |
| `kb_pipeline/definitions.py` | `Definitions`; run-queue limits documented in the docstring |

Known gaps the Phase 2 port must close (deliberately left visible in the draft, marked `GAP:`):

1. Calls Microsoft Graph and Elasticsearch directly instead of through ports.
2. Holds whole files in memory (`resp.content`); must stream to the raw store.
3. Expired delta token (`410 Gone`, `resyncRequired`) is only logged, never resynced.
4. Deletion flips the registry before Elasticsearch; the brief requires ES visibility first.
5. Unsupported / oversized files are silently skipped instead of raising `exception_item`.
6. No parse, PII gate, chunk, embed or index steps; no dry run, purge or upload sensor.
7. Uses `UPDATE kb.document` as a superuser-style connection; must connect as `kb_pipeline`.
