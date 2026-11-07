"""Ingestion engine: everything the Dagster jobs do, as plain Python behind the ports.

Invariants (CLAUDE.md sections 2 and 7):
- idempotent: deterministic doc/version/chunk ids; an unchanged item (same cTag) is a no-op; the same
  bad file raises one exception, not one per run;
- the delta token is committed only after the whole run succeeded;
- versioning: new chunks are indexed hidden, Elasticsearch visibility is switched in one request,
  THEN the registry flips (a crash between leaves the registry authoritative; index_sync repairs);
- deletion hides in Elasticsearch first, then marks the registry; the raw copy is retained until its
  retention date; purge refuses anything under legal hold (also enforced by a DB trigger);
- the pipeline connects as kb_pipe (role kb_pipeline): it can never take governance decisions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from kb.adapters.local.registry import PostgresRegistry
from kb.ports import ChunkIndex, DocumentSourceFactory, Embedder, MalwareScanner, RawStore
from kb.ports.types import ChangeKind, Chunk, SourceItem
from kb.processing.chunk import chunk_document, chunk_id
from kb.processing.filetype import declared_type
from kb.processing.parse import DoclingParser, ParseError
from kb.processing.pii import PiiGate, PiiPolicy

log = logging.getLogger("kb.ingest")
ACTOR = "svc:pipeline"


class RunConflictError(RuntimeError):
    """Another run for this source is queued or running (DB-enforced: one in-flight run per source)."""


def doc_id_for(source_id: str, external_item_id: str) -> str:
    return hashlib.sha256(f"{source_id}|{external_item_id}".encode()).hexdigest()[:32]


@dataclass
class RunStats:
    counts: Counter[str] = field(default_factory=Counter)
    exceptions: list[dict[str, Any]] = field(default_factory=list)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def as_json(self) -> dict[str, Any]:
        return {"counts": dict(sorted(self.counts.items())), "exceptions": self.exceptions[:200]}


@dataclass(frozen=True)
class SourceCtx:
    source_id: str
    bu: str
    source_type: str
    status: str
    sp_site_id: str | None
    sp_drive_id: str | None
    sp_folder_path: str | None
    revision_id: str
    acl_groups: list[str]
    allowed_extensions: list[str]
    max_file_bytes: int
    retention_class: str
    pii_policy: dict[str, Any]


class IngestEngine:
    def __init__(
        self,
        *,
        registry: PostgresRegistry,
        sources: DocumentSourceFactory,
        raw: RawStore,
        processed: RawStore,
        scanner: MalwareScanner,
        index: ChunkIndex,
        embedder: Embedder,
        parser: DoclingParser,
        pii: PiiGate,
        processing: dict[str, Any],
    ) -> None:
        self.reg = registry
        self.sources = sources
        self.raw = raw
        self.processed = processed
        self.scanner = scanner
        self.index = index
        self.embedder = embedder
        self.parser = parser
        self.pii = pii
        self.cfg = processing

    # ================================================================== helpers
    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with self.reg.transaction(ACTOR) as conn:
            yield conn

    def source_ctx(self, source_id: str, revision_id: str | None = None) -> SourceCtx:
        with self.tx() as c:
            row = c.execute(
                """SELECT s.source_id, s.bu, s.source_type, s.status, s.sp_site_id, s.sp_drive_id, s.sp_folder_path,
                          r.revision_id, r.acl_groups, r.allowed_extensions, r.max_file_bytes, r.retention_class,
                          r.pii_policy
                   FROM kb.kb_source s
                   JOIN kb.source_revision r ON r.revision_id = coalesce(%s::uuid, s.current_revision_id)
                   WHERE s.source_id = %s""",
                (revision_id, source_id),
            ).fetchone()
        if row is None:
            raise LookupError(f"source {source_id} has no revision {revision_id or '(current)'}")
        row["revision_id"] = str(row["revision_id"])
        return SourceCtx(**row)

    def _start_run(self, src: SourceCtx, kind: str) -> str:
        try:
            with self.tx() as c:
                row = c.execute(
                    """INSERT INTO kb.ingestion_run (source_id, revision_id, run_kind, status, started_at)
                       VALUES (%s, %s, %s, 'RUNNING', now()) RETURNING run_id""",
                    (src.source_id, src.revision_id, kind),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise RunConflictError(f"a run for {src.source_id} is already queued or running") from None
        assert row is not None
        return str(row["run_id"])

    def _finish_run(self, run_id: str, status: str, stats: RunStats, error: str | None = None) -> None:
        payload = stats.as_json() | ({"error": error} if error else {})
        with self.tx() as c:
            c.execute(
                "UPDATE kb.ingestion_run SET status = %s, finished_at = now(), stats = %s WHERE run_id = %s",
                (status, Jsonb(payload), run_id),
            )

    @contextmanager
    def run(self, src: SourceCtx, kind: str) -> Iterator[tuple[str, RunStats]]:
        run_id = self._start_run(src, kind)
        stats = RunStats()
        try:
            yield run_id, stats
        except Exception as e:
            log.exception("run %s (%s %s) failed", run_id, kind, src.source_id)
            self._finish_run(run_id, "FAILED", stats, f"{type(e).__name__}: {e}")
            raise
        self._finish_run(run_id, "SUCCEEDED", stats)

    def _raise_exception(
        self,
        src: SourceCtx,
        run_id: str | None,
        reason: str,
        detail: dict[str, Any],
        stats: RunStats,
        *,
        item: SourceItem | None = None,
        doc_id: str | None = None,
        version_id: str | None = None,
        notify: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO kb.exception_item (doc_id, version_id, run_id, source_id, reason, detail,
                                                  external_item_id, ctag)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (
                    doc_id,
                    version_id,
                    run_id,
                    src.source_id,
                    reason,
                    Jsonb(detail),
                    item.external_item_id if item else doc_id,
                    item.ctag if item else version_id,
                ),
            )
            if notify:
                c.execute(
                    "INSERT INTO kb.notification (user_id, kind, message, ref) VALUES (%s, 'UPLOAD_REJECTED', %s, %s)",
                    (
                        notify,
                        f"{reason}: {detail.get('message', '')}",
                        Jsonb({"doc_id": doc_id, "version_id": version_id, "reason": reason}),
                    ),
                )
        stats.add(f"exception:{reason}")
        stats.exceptions.append({"reason": reason, "item": item.path if item else doc_id, **detail})

    # ================================================================== dry run
    def dry_run(self, source_id: str, revision_id: str) -> dict[str, Any]:
        """List and classify without downloading or writing documents/index. Report goes on the run."""
        src = self.source_ctx(source_id, revision_id)
        bound = self._bind(src)
        with self.run(src, "DRY_RUN") as (_, stats):
            by_ext: Counter[str] = Counter()
            for item in bound.list_all():
                ext = declared_type(item.name)
                by_ext[ext or "(none)"] += 1
                stats.add("items")
                if ext not in src.allowed_extensions:
                    stats.add("would_except:UNSUPPORTED_TYPE")
                    stats.exceptions.append({"reason": "UNSUPPORTED_TYPE", "item": item.path})
                elif item.size_bytes > src.max_file_bytes:
                    stats.add("would_except:SIZE_LIMIT")
                    stats.exceptions.append({"reason": "SIZE_LIMIT", "item": item.path, "size": item.size_bytes})
                else:
                    stats.add("would_index")
                    stats.add("would_index_bytes", item.size_bytes)
            stats.counts.update({f"ext:{k}": v for k, v in by_ext.items()})
        return stats.as_json()

    # ================================================================== sync (first load / delta / reconcile)
    def _bind(self, src: SourceCtx) -> Any:
        if src.source_type != "SHAREPOINT":
            raise ValueError(f"{src.source_id} is not a SharePoint source")
        assert src.sp_site_id and src.sp_drive_id and src.sp_folder_path
        return self.sources.bind(src.sp_site_id, src.sp_drive_id, src.sp_folder_path)

    def sync(self, source_id: str, kind: str = "DELTA") -> dict[str, Any]:
        """FIRST_LOAD / DELTA / RECONCILE. An expired token turns a DELTA into a reconcile."""
        with self.tx() as c:
            row = c.execute("SELECT status FROM kb.kb_source WHERE source_id = %s", (source_id,)).fetchone()
        if row is None or row["status"] != "ACTIVE":
            raise RuntimeError(f"source {source_id} is {row['status'] if row else 'unknown'}, not ACTIVE")
        src = self.source_ctx(source_id)
        bound = self._bind(src)
        with self.tx() as c:
            st = c.execute("SELECT delta_token FROM kb.sync_state WHERE source_id = %s", (source_id,)).fetchone()
        token = st["delta_token"] if st and kind == "DELTA" else None
        with self.run(src, kind) as (run_id, stats):
            pages = list(bound.delta(token))
            if any(p.resync_required for p in pages):
                stats.add("resync_required")
                pages = list(bound.delta(None))
                kind = "RECONCILE"
            seen: set[str] = set()
            for page in pages:
                for item in page.items:
                    seen.add(item.external_item_id)
                    self._apply(src, run_id, bound, item, stats)
            if token is None:  # full listing: anything the registry has that the source no longer lists is gone
                self._remove_missing(src, seen, stats)
            new_token = pages[-1].next_token if pages else token
            with self.tx() as c:  # commit the token only now, after every item succeeded
                c.execute(
                    """INSERT INTO kb.sync_state (source_id, delta_token, last_run_id, last_success_at)
                       VALUES (%s, %s, %s, now())
                       ON CONFLICT (source_id) DO UPDATE SET delta_token = EXCLUDED.delta_token,
                           last_run_id = EXCLUDED.last_run_id, last_success_at = now()""",
                    (source_id, new_token, run_id),
                )
            stats.counts["token_committed"] = 1
        return stats.as_json()

    def _apply(self, src: SourceCtx, run_id: str, bound: Any, item: SourceItem, stats: RunStats) -> None:
        doc_id = doc_id_for(src.source_id, item.external_item_id)
        if item.change is ChangeKind.DELETED:
            self._hide_document(doc_id, "DELETED", stats)
            return
        if item.change is ChangeKind.OUT_OF_SCOPE:
            self._hide_document(doc_id, "OUT_OF_SCOPE", stats)
            return
        with self.tx() as c:
            cur = c.execute(
                """SELECT d.status, d.current_path, v.version_id, v.version_no, v.ctag, v.sha256
                   FROM kb.document d LEFT JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current
                   WHERE d.doc_id = %s""",
                (doc_id,),
            ).fetchone()
            # this exact content (cTag) already produced an exception: one exception, not one per run
            tried = c.execute(
                """SELECT 1 FROM kb.exception_item WHERE source_id = %s AND external_item_id = %s AND ctag = %s
                   UNION ALL
                   SELECT 1 FROM kb.document_version WHERE doc_id = %s AND ctag = %s AND status = 'EXCEPTION'""",
                (src.source_id, item.external_item_id, item.ctag, doc_id, item.ctag),
            ).fetchone()
            # same content hidden earlier (deleted / moved out of scope): restore instead of re-ingesting
            restorable = c.execute(
                """SELECT v.version_id FROM kb.document_version v JOIN kb.document d USING (doc_id)
                   WHERE v.doc_id = %s AND v.ctag = %s AND v.status = 'DELETED'
                     AND d.status IN ('OUT_OF_SCOPE', 'DELETED')
                   ORDER BY v.version_no DESC LIMIT 1""",
                (doc_id, item.ctag),
            ).fetchone()
        if cur and cur["ctag"] == item.ctag and cur["status"] == "ACTIVE":
            if cur["current_path"] != item.path:
                with self.tx() as c:
                    c.execute("UPDATE kb.document SET current_path = %s WHERE doc_id = %s", (item.path, doc_id))
                stats.add("moved_within_scope")
            else:
                stats.add("unchanged")
            return
        if restorable:
            self._restore_document(doc_id, restorable["version_id"], src, stats)  # moved back into scope
            return
        if tried:
            stats.add("unchanged_exception")
            return
        # gates that need no download
        ext = declared_type(item.name)
        if ext not in src.allowed_extensions:
            self._raise_exception(src, run_id, "UNSUPPORTED_TYPE", {"message": f".{ext} not allowed"}, stats, item=item)
            return
        if item.size_bytes > src.max_file_bytes:
            self._raise_exception(
                src,
                run_id,
                "SIZE_LIMIT",
                {"message": f"{item.size_bytes} > {src.max_file_bytes}", "size": item.size_bytes},
                stats,
                item=item,
            )
            return
        version_no = (cur["version_no"] + 1) if cur and cur["version_no"] else self._next_version(doc_id)
        key = f"{src.bu}/{src.source_id}/{doc_id}/v{version_no}/{PurePosixPath(item.name).name}"
        with bound.open(item) as stream:
            stored = self.raw.put_stream(key, stream)
        if cur and cur["sha256"] == stored.sha256:  # metadata-only change (e.g. cTag churn): keep version
            with self.tx() as c:
                c.execute(
                    "UPDATE kb.document_version SET ctag = %s WHERE version_id = %s", (item.ctag, cur["version_id"])
                )
            self.raw.delete(stored.uri)
            stats.add("unchanged_content")
            return
        with self.raw.open(stored.uri) as f:
            scan = self.scanner.scan(f, item.name)
        version_id = f"{doc_id}:v{version_no}"
        with self.tx() as c:
            if cur is None:
                c.execute(
                    """INSERT INTO kb.document (doc_id, source_id, external_item_id, current_path, title)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (doc_id) DO NOTHING""",
                    (doc_id, src.source_id, item.external_item_id, item.path, PurePosixPath(item.name).stem),
                )
            c.execute(
                """INSERT INTO kb.document_version (version_id, doc_id, version_no, ctag, sha256, size_bytes,
                                                    mime_type, raw_uri, status, run_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PROCESSING', %s)
                   ON CONFLICT (version_id) DO UPDATE SET ctag = EXCLUDED.ctag, sha256 = EXCLUDED.sha256,
                       raw_uri = EXCLUDED.raw_uri, status = 'PROCESSING', run_id = EXCLUDED.run_id""",
                (
                    version_id,
                    doc_id,
                    version_no,
                    item.ctag,
                    stored.sha256,
                    stored.size_bytes,
                    scan.detected_type,
                    stored.uri,
                    run_id,
                ),
            )
        if not scan.clean:
            reason = "TYPE_MISMATCH" if scan.detected_type != ext else "MALWARE"
            self._version_exception(src, run_id, doc_id, version_id, reason, {"message": scan.reason}, stats, item=item)
            return
        self.process_version(src, run_id, doc_id, version_id, stats, item=item)

    def _next_version(self, doc_id: str) -> int:
        with self.tx() as c:
            r = c.execute(
                "SELECT coalesce(max(version_no), 0) + 1 AS n FROM kb.document_version WHERE doc_id = %s", (doc_id,)
            ).fetchone()
        assert r is not None
        return int(r["n"])

    def _version_exception(
        self,
        src: SourceCtx,
        run_id: str | None,
        doc_id: str,
        version_id: str,
        reason: str,
        detail: dict[str, Any],
        stats: RunStats,
        *,
        item: SourceItem | None = None,
        notify: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE kb.document_version SET status = 'EXCEPTION', processing = processing || %s "
                "WHERE version_id = %s",
                (Jsonb({"exception": reason, **detail}), version_id),
            )
        self._raise_exception(
            src, run_id, reason, detail, stats, item=item, doc_id=doc_id, version_id=version_id, notify=notify
        )

    # ================================================================== processing one version
    def process_version(
        self,
        src: SourceCtx,
        run_id: str | None,
        doc_id: str,
        version_id: str,
        stats: RunStats,
        *,
        item: SourceItem | None = None,
        skip_pii: bool = False,
        notify: str | None = None,
    ) -> bool:
        with self.tx() as c:
            v = c.execute(
                """SELECT v.*, d.origin, d.uploaded_by, d.visibility, d.title, d.source_id
                   FROM kb.document_version v JOIN kb.document d USING (doc_id) WHERE v.version_id = %s""",
                (version_id,),
            ).fetchone()
        assert v is not None
        filename = v["original_filename"] or (item.name if item else f"{doc_id}.{v['mime_type']}")
        with tempfile.TemporaryDirectory(prefix="kb-parse-") as tmp:
            local = Path(tmp) / PurePosixPath(filename).name
            with self.raw.open(v["raw_uri"]) as fin, open(local, "wb") as fout:
                while block := fin.read(1 << 20):
                    fout.write(block)
            try:
                parsed = self.parser.parse(local)
            except ParseError as e:
                self._version_exception(
                    src,
                    run_id,
                    doc_id,
                    version_id,
                    "PARSE_FAILURE",
                    {"message": str(e)[:500]},
                    stats,
                    item=item,
                    notify=notify,
                )
                return False
        doc = parsed.document
        policy = PiiPolicy.merged(self.cfg["pii"], src.pii_policy)
        report = self.pii.check(doc.export_to_text(), policy)
        if report.exceeded and not skip_pii:
            self._version_exception(
                src,
                run_id,
                doc_id,
                version_id,
                "PII_THRESHOLD",
                {"message": f"{report.total} PII entities > {policy.max_entities}", **report.as_detail(policy)},
                stats,
                item=item,
                notify=notify,
            )
            return False
        drafts = chunk_document(doc, self.embedder.tokenizer, int(self.cfg["chunk_max_tokens"]))  # type: ignore[attr-defined]
        vectors = self.embedder.embed_documents([d.text for d in drafts])
        acl = self.desired_acl(src, v["origin"], v["visibility"], v["uploaded_by"])
        title = doc.name if v["origin"] == "UPLOAD" else v["title"]
        chunks = [
            Chunk(
                chunk_id=chunk_id(version_id, d.chunk_no),
                doc_id=doc_id,
                version_id=version_id,
                text=d.text,
                embedding=vec,
                acl_groups=acl,
                bu=src.bu,
                is_current=False,  # hidden until the visibility switch below
                source_active=src.status == "ACTIVE",
                page=d.page_start,
                section_path=d.section_path,
                metadata={
                    "source_id": src.source_id,
                    "version_no": v["version_no"],
                    "origin": v["origin"],
                    "title": v["title"] or title,
                    "page_end": d.page_end,
                    "content_sha": d.content_sha,
                    "chunk_no": d.chunk_no,
                    "doc_items": {"items": d.doc_items},
                },
            )
            for d, vec in zip(drafts, vectors, strict=True)
        ]
        self.processed.put_stream(
            f"{version_id.replace(':', '_')}/docling.json", _bytes_io(json.dumps(doc.export_to_dict()).encode())
        )
        self.index.upsert(chunks)
        self._flip_current(
            src,
            doc_id,
            version_id,
            len(chunks),
            acl,
            report.as_detail(policy)
            | {
                "pages": parsed.pages,
                "chunks": len(chunks),
                "pii_overridden": bool(report.exceeded and skip_pii),
                "parse_warnings": parsed.warnings[:20],
            },
        )
        stats.add("indexed_versions")
        stats.add("indexed_chunks", len(chunks))
        return True

    def desired_acl(self, src: SourceCtx, origin: str, visibility: str | None, uploader: str | None) -> list[str]:
        if origin == "UPLOAD" and visibility != "PUBLISHED":
            return [f"user:{uploader}"]
        return list(src.acl_groups)

    def _flip_current(
        self, src: SourceCtx, doc_id: str, version_id: str, n_chunks: int, acl: list[str], processing: dict[str, Any]
    ) -> None:
        self.index.set_visibility(doc_id, current_version_id=version_id)  # 1) search sees the new version only
        now = datetime.now(UTC)
        with self.tx() as c:  # 2) registry flip, one transaction
            c.execute(
                """UPDATE kb.document_version SET is_current = false, status = 'SUPERSEDED',
                       not_current_since = %s, retention_until = kb.retention_until(%s, %s)
                   WHERE doc_id = %s AND is_current AND version_id <> %s""",
                (now, src.retention_class, now, doc_id, version_id),
            )
            c.execute(
                """UPDATE kb.document_version SET is_current = true, status = 'INDEXED', indexed_at = %s,
                       chunk_count = %s, processing = processing || %s
                   WHERE version_id = %s""",
                (now, n_chunks, Jsonb(processing), version_id),
            )
            c.execute(
                """UPDATE kb.document SET current_version_no = split_part(%s, ':v', 2)::int, status = 'ACTIVE',
                       deleted_at = NULL, acl_stamp = %s, active_stamp = %s
                   WHERE doc_id = %s""",
                (version_id, acl, src.status == "ACTIVE", doc_id),
            )

    def _hide_document(self, doc_id: str, status: str, stats: RunStats) -> None:
        with self.tx() as c:
            d = c.execute("SELECT status, source_id FROM kb.document WHERE doc_id = %s", (doc_id,)).fetchone()
            src_row = (
                c.execute(
                    """SELECT r.retention_class FROM kb.kb_source s JOIN kb.source_revision r
                   ON r.revision_id = s.current_revision_id WHERE s.source_id = %s""",
                    (d["source_id"],),
                ).fetchone()
                if d
                else None
            )
        if d is None:
            stats.add(f"{status.lower()}_unknown")  # never indexed (e.g. it was an exception): nothing to hide
            return
        if d["status"] != "ACTIVE":
            stats.add(f"{status.lower()}_already")
            return
        self.index.set_visibility(doc_id, current_version_id=None)  # 1) out of search
        now = datetime.now(UTC)
        with self.tx() as c:  # 2) registry; raw copy is kept until retention_until
            c.execute(
                """UPDATE kb.document_version SET is_current = false, status = 'DELETED', not_current_since = %s,
                       retention_until = kb.retention_until(%s, %s)
                   WHERE doc_id = %s AND is_current""",
                (now, src_row["retention_class"] if src_row else "PERM", now, doc_id),
            )
            c.execute("UPDATE kb.document SET status = %s, deleted_at = %s WHERE doc_id = %s", (status, now, doc_id))
        stats.add(status.lower())

    def _restore_document(self, doc_id: str, version_id: str, src: SourceCtx, stats: RunStats) -> None:
        self.index.set_visibility(doc_id, current_version_id=version_id)
        with self.tx() as c:
            c.execute(
                """UPDATE kb.document_version SET is_current = true, status = 'INDEXED', not_current_since = NULL,
                             retention_until = NULL WHERE version_id = %s""",
                (version_id,),
            )
            c.execute("UPDATE kb.document SET status = 'ACTIVE', deleted_at = NULL WHERE doc_id = %s", (doc_id,))
        stats.add("restored")

    def _remove_missing(self, src: SourceCtx, seen: set[str], stats: RunStats) -> None:
        with self.tx() as c:
            rows = c.execute(
                "SELECT doc_id, external_item_id FROM kb.document WHERE source_id = %s AND status = 'ACTIVE'",
                (src.source_id,),
            ).fetchall()
        for r in rows:
            if r["external_item_id"] not in seen:
                self._hide_document(r["doc_id"], "DELETED", stats)

    # ================================================================== uploads
    def process_upload(self, version_id: str) -> dict[str, Any]:
        with self.tx() as c:
            v = c.execute(
                """SELECT v.version_id, v.doc_id, v.status, v.raw_uri, v.original_filename, d.source_id,
                          d.uploaded_by
                   FROM kb.document_version v JOIN kb.document d USING (doc_id) WHERE v.version_id = %s""",
                (version_id,),
            ).fetchone()
        if v is None or v["status"] != "RECEIVED":
            return {"skipped": version_id}
        src = self.source_ctx(v["source_id"])
        with self.run(src, "UPLOAD") as (run_id, stats):
            with self.tx() as c:
                c.execute(
                    "UPDATE kb.document_version SET status = 'PROCESSING', run_id = %s WHERE version_id = %s",
                    (run_id, version_id),
                )
            with self.raw.open(v["raw_uri"]) as f:
                scan = self.scanner.scan(f, v["original_filename"])
            if not scan.clean:
                ext = declared_type(v["original_filename"])
                reason = "TYPE_MISMATCH" if scan.detected_type != ext else "MALWARE"
                self._version_exception(
                    src,
                    run_id,
                    v["doc_id"],
                    version_id,
                    reason,
                    {"message": scan.reason},
                    stats,
                    notify=v["uploaded_by"],
                )
            else:
                self.process_version(src, run_id, v["doc_id"], version_id, stats, notify=v["uploaded_by"])
        return stats.as_json()

    # ================================================================== released exceptions
    def reprocess_released(self, exception_id: str) -> dict[str, Any]:
        with self.tx() as c:
            e = c.execute(
                "SELECT * FROM kb.exception_item WHERE exception_id = %s AND status = 'RELEASED' "
                "AND reprocessed_at IS NULL",
                (exception_id,),
            ).fetchone()
        if e is None or e["version_id"] is None:
            return {"skipped": exception_id}
        src = self.source_ctx(e["source_id"])
        with self.tx() as c:
            up = c.execute("SELECT uploaded_by FROM kb.document WHERE doc_id = %s", (e["doc_id"],)).fetchone()
        with self.run(src, "REPROCESS") as (run_id, stats):
            ok = self.process_version(
                src,
                run_id,
                e["doc_id"],
                e["version_id"],
                stats,
                skip_pii=e["reason"] == "PII_THRESHOLD",
                notify=up["uploaded_by"] if up else None,
            )
            with self.tx() as c:
                c.execute(
                    "UPDATE kb.exception_item SET reprocessed_at = now(), reprocess_run_id = %s "
                    "WHERE exception_id = %s",
                    (run_id, exception_id),
                )
            stats.add("reprocessed_ok" if ok else "reprocessed_failed")
        return stats.as_json()

    # ================================================================== index sync (ACL / source-active / deletes)
    def index_sync(self) -> dict[str, Any]:
        """Converge the index to the registry: publish/unpublish re-stamps, approved ACL changes, suspended
        sources, uploader deletions. Never re-embeds."""
        stats = RunStats()
        with self.tx() as c:
            rows = c.execute(
                """SELECT d.doc_id, d.status, d.origin, d.visibility, d.uploaded_by, d.acl_stamp, d.active_stamp,
                          s.source_id, s.status AS source_status, r.acl_groups
                   FROM kb.document d
                   JOIN kb.kb_source s USING (source_id)
                   JOIN kb.source_revision r ON r.revision_id = s.current_revision_id
                   JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current AND v.status = 'INDEXED'"""
            ).fetchall()
        for r in rows:
            if r["status"] != "ACTIVE":
                self._hide_document_any(r["doc_id"], r["status"], stats)
                continue
            want = (
                [f"user:{r['uploaded_by']}"]
                if r["origin"] == "UPLOAD" and r["visibility"] != "PUBLISHED"
                else list(r["acl_groups"])
            )
            if list(r["acl_stamp"] or []) != want:
                self.index.restamp_acl(r["doc_id"], want)
                with self.tx() as c:
                    c.execute("UPDATE kb.document SET acl_stamp = %s WHERE doc_id = %s", (want, r["doc_id"]))
                stats.add("acl_restamped")
            active = r["source_status"] == "ACTIVE"
            if r["active_stamp"] is not active:
                self.index.set_source_active(r["source_id"], active)
                with self.tx() as c:
                    c.execute("UPDATE kb.document SET active_stamp = %s WHERE source_id = %s", (active, r["source_id"]))
                stats.add("source_active_restamped")
        return stats.as_json()

    def _hide_document_any(self, doc_id: str, status: str, stats: RunStats) -> None:
        """A document already marked non-ACTIVE in the registry (e.g. uploader delete) still shows a current
        version: hide it and close the version."""
        self.index.set_visibility(doc_id, current_version_id=None)
        now = datetime.now(UTC)
        with self.tx() as c:
            c.execute(
                """UPDATE kb.document_version v SET is_current = false, status = 'DELETED', not_current_since = %s,
                       retention_until = kb.retention_until(r.retention_class, %s)
                   FROM kb.document d JOIN kb.kb_source s USING (source_id)
                   JOIN kb.source_revision r ON r.revision_id = s.current_revision_id
                   WHERE v.doc_id = d.doc_id AND d.doc_id = %s AND v.is_current""",
                (now, now, doc_id),
            )
        stats.add(f"hidden_{status.lower()}")

    # ================================================================== retention purge
    def purge(self, today: date | None = None) -> dict[str, Any]:
        """Delete raw + chunks of versions past retention; write a purge certificate for each.
        Anything under legal hold is skipped (and the DB trigger would refuse it anyway)."""
        today = today or datetime.now(UTC).date()
        stats = RunStats()
        with self.tx() as c:
            rows = c.execute(
                """SELECT v.version_id, v.doc_id, v.raw_uri, v.sha256, v.retention_until, d.source_id,
                          EXISTS (SELECT 1 FROM kb.legal_hold h WHERE h.released_at IS NULL
                                  AND (h.doc_id = d.doc_id OR h.source_id = d.source_id)) AS held
                   FROM kb.document_version v JOIN kb.document d USING (doc_id)
                   WHERE NOT v.is_current AND v.status IN ('SUPERSEDED', 'DELETED')
                     AND v.retention_until IS NOT NULL AND v.retention_until <= %s""",
                (today,),
            ).fetchall()
        for r in rows:
            if r["held"]:
                stats.add("held")
                continue
            with self.tx() as c:  # certificate + status in one transaction; deletes happen inside it
                c.execute(
                    """INSERT INTO kb.purge_certificate (doc_id, version_id, raw_uri, sha256, retention_until,
                                                         purged_by, evidence)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        r["doc_id"],
                        r["version_id"],
                        r["raw_uri"],
                        r["sha256"],
                        r["retention_until"],
                        ACTOR,
                        Jsonb({"raw_existed": self.raw.exists(r["raw_uri"]), "index": "chunks deleted"}),
                    ),
                )
                c.execute("UPDATE kb.document_version SET status = 'PURGED' WHERE version_id = %s", (r["version_id"],))
                self.index.delete_version(r["version_id"])
                if self.raw.exists(r["raw_uri"]):
                    self.raw.delete(r["raw_uri"])
            stats.add("purged")
        return stats.as_json()


def _bytes_io(data: bytes) -> Any:
    import io

    return io.BytesIO(data)
