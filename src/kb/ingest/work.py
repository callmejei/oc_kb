"""What the sensors and schedules look for. Plain queries, testable without Dagster."""

from __future__ import annotations

import hashlib
from typing import Any

from kb.ingest.engine import IngestEngine


def pending_dry_runs(engine: IngestEngine) -> list[dict[str, Any]]:
    """Submitted onboarding/change revisions that have no dry-run yet (the checker needs the report)."""
    with engine.tx() as c:
        return c.execute(
            """SELECT a.source_id, a.revision_id::text AS revision_id FROM kb.approval_request a
               JOIN kb.kb_source s USING (source_id)
               WHERE a.status = 'PENDING' AND a.request_type IN ('ONBOARD_SOURCE', 'CHANGE_SOURCE')
                 AND s.source_type = 'SHAREPOINT'
                 AND NOT EXISTS (SELECT 1 FROM kb.ingestion_run r WHERE r.revision_id = a.revision_id
                                   AND r.run_kind = 'DRY_RUN')"""
        ).fetchall()


def sources_needing_first_load(engine: IngestEngine) -> list[str]:
    with engine.tx() as c:
        rows = c.execute(
            """SELECT s.source_id FROM kb.kb_source s LEFT JOIN kb.sync_state t USING (source_id)
               WHERE s.status = 'ACTIVE' AND s.source_type = 'SHAREPOINT' AND t.source_id IS NULL"""
        ).fetchall()
    return [r["source_id"] for r in rows]


def active_sharepoint_sources(engine: IngestEngine) -> list[str]:
    with engine.tx() as c:
        rows = c.execute(
            "SELECT source_id FROM kb.kb_source WHERE status = 'ACTIVE' AND source_type = 'SHAREPOINT' "
            "ORDER BY source_id"
        ).fetchall()
    return [r["source_id"] for r in rows]


def received_uploads(engine: IngestEngine) -> list[dict[str, Any]]:
    with engine.tx() as c:
        return c.execute(
            """SELECT v.version_id, d.source_id FROM kb.document_version v JOIN kb.document d USING (doc_id)
               WHERE v.status = 'RECEIVED' AND d.origin = 'UPLOAD' ORDER BY v.created_at"""
        ).fetchall()


def released_exceptions(engine: IngestEngine) -> list[dict[str, Any]]:
    with engine.tx() as c:
        return c.execute(
            """SELECT exception_id::text AS exception_id, source_id FROM kb.exception_item
               WHERE status = 'RELEASED' AND reprocessed_at IS NULL AND version_id IS NOT NULL"""
        ).fetchall()


def index_drift(engine: IngestEngine) -> list[str]:
    """Documents whose index stamp disagrees with the registry (publish/unpublish, ACL change, suspend,
    uploader delete). Returns doc ids; empty means the index is converged."""
    with engine.tx() as c:
        rows = c.execute(
            """SELECT d.doc_id FROM kb.document d
               JOIN kb.kb_source s USING (source_id)
               JOIN kb.source_revision r ON r.revision_id = s.current_revision_id
               JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current AND v.status = 'INDEXED'
               WHERE d.status <> 'ACTIVE'
                  OR d.active_stamp IS DISTINCT FROM (s.status = 'ACTIVE')
                  OR d.acl_stamp IS DISTINCT FROM (
                       CASE WHEN d.origin = 'UPLOAD' AND d.visibility <> 'PUBLISHED'
                            THEN ARRAY['user:' || d.uploaded_by] ELSE r.acl_groups END)
               ORDER BY d.doc_id"""
        ).fetchall()
    return [r["doc_id"] for r in rows]


def drift_key(doc_ids: list[str]) -> str:
    return hashlib.sha256("|".join(doc_ids).encode()).hexdigest()[:16]
