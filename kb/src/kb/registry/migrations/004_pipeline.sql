-- 004_pipeline: what the ingestion pipeline needs (CLAUDE.md section 7, Phase 2). Extends 001-003;
-- every earlier guarantee is kept. New guarantees:
--   P1 retention classes are governed reference data; a revision may only use a known class
--   P2 only PII_THRESHOLD and PARSE_FAILURE exceptions can be released (RELEASE_EXCEPTION);
--      the rest can only be excluded (fix the file or the source configuration instead)
--   P3 uploads are registered only through kb.register_upload (as the uploader), with
--      per-BU duplicate detection that never reveals someone else's private upload
--   P4 retention is per version and starts when the version stops being current

-- ---------------------------------------------------------------------------- retention classes
CREATE TABLE kb.retention_class (
  code        text PRIMARY KEY,
  days        int  CHECK (days IS NULL OR days >= 0),   -- NULL = permanent, never purged
  description text NOT NULL
);
INSERT INTO kb.retention_class VALUES
  ('R0D',  0,    'synthetic test class: purge as soon as not current'),
  ('R1Y',  365,  'one year after a version stops being current'),
  ('R2Y',  730,  'two years'),
  ('R7Y',  2555, 'seven years'),
  ('R10Y', 3650, 'ten years'),
  ('PERM', NULL, 'permanent');
ALTER TABLE kb.source_revision
  ADD CONSTRAINT source_revision_retention_class_fk FOREIGN KEY (retention_class) REFERENCES kb.retention_class(code);

CREATE OR REPLACE FUNCTION kb.retention_until(p_class text, p_from timestamptz) RETURNS date
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN rc.days IS NULL THEN NULL ELSE (p_from AT TIME ZONE 'UTC')::date + rc.days END
  FROM kb.retention_class rc WHERE rc.code = p_class
$$;

-- ---------------------------------------------------------------------------- versions / documents
ALTER TABLE kb.document_version DROP CONSTRAINT document_version_status_check;
ALTER TABLE kb.document_version ADD CONSTRAINT document_version_status_check CHECK (status IN (
  'RECEIVED', 'PROCESSING', 'INDEXED', 'EXCEPTION', 'SUPERSEDED', 'DELETED', 'PURGED', 'LINKED'));
ALTER TABLE kb.document_version
  ADD COLUMN not_current_since timestamptz,      -- P4: retention clock start
  ADD COLUMN retention_until   date,
  ADD COLUMN processing        jsonb NOT NULL DEFAULT '{}'::jsonb;   -- parse/chunk/PII report

ALTER TABLE kb.document
  ADD COLUMN acl_stamp      text[],              -- ACL the index currently carries for this document
  ADD COLUMN active_stamp   boolean;             -- source_active the index currently carries

-- ---------------------------------------------------------------------------- exceptions
ALTER TABLE kb.exception_item DROP CONSTRAINT exception_item_reason_check;
ALTER TABLE kb.exception_item ADD CONSTRAINT exception_item_reason_check CHECK (reason IN (
  'UNSUPPORTED_TYPE', 'SIZE_LIMIT', 'PARSE_FAILURE', 'PII_THRESHOLD', 'TYPE_MISMATCH', 'MALWARE'));
ALTER TABLE kb.exception_item
  ADD COLUMN external_item_id text,
  ADD COLUMN ctag             text,
  ADD COLUMN reprocessed_at   timestamptz,
  ADD COLUMN reprocess_run_id uuid REFERENCES kb.ingestion_run(run_id);
-- idempotent: re-running a delta over the same unchanged bad file does not raise a second exception
CREATE UNIQUE INDEX exception_item_once
  ON kb.exception_item (source_id, external_item_id, coalesce(ctag, ''), reason)
  WHERE external_item_id IS NOT NULL;

CREATE OR REPLACE FUNCTION kb.releasable_reason(p_reason text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$ SELECT p_reason IN ('PII_THRESHOLD', 'PARSE_FAILURE') $$;

-- P2: a RELEASE_EXCEPTION request must target an OPEN, releasable exception of the same source
CREATE OR REPLACE FUNCTION kb.tg_release_request_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE e kb.exception_item;
BEGIN
  IF NEW.request_type <> 'RELEASE_EXCEPTION' THEN
    RETURN NEW;
  END IF;
  SELECT * INTO e FROM kb.exception_item WHERE exception_id = NEW.exception_id;
  IF e.exception_id IS NULL OR e.source_id <> NEW.source_id THEN
    RAISE EXCEPTION 'exception % does not belong to source %', NEW.exception_id, NEW.source_id;
  END IF;
  IF e.status <> 'OPEN' THEN
    RAISE EXCEPTION 'exception % is %, not OPEN', e.exception_id, e.status;
  END IF;
  IF NOT kb.releasable_reason(e.reason) THEN
    RAISE EXCEPTION '% exceptions cannot be released; exclude it and fix the file or source configuration',
      e.reason USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approval_request_release_guard BEFORE INSERT ON kb.approval_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_release_request_guard();
CREATE UNIQUE INDEX approval_request_one_pending_release
  ON kb.approval_request (exception_id) WHERE status = 'PENDING' AND request_type = 'RELEASE_EXCEPTION';

-- ---------------------------------------------------------------------------- runs
ALTER TABLE kb.ingestion_run DROP CONSTRAINT ingestion_run_run_kind_check;
ALTER TABLE kb.ingestion_run ADD CONSTRAINT ingestion_run_run_kind_check CHECK (run_kind IN (
  'DRY_RUN', 'FIRST_LOAD', 'DELTA', 'RECONCILE', 'PURGE', 'UPLOAD', 'REPROCESS', 'INDEX_SYNC'));

-- ---------------------------------------------------------------------------- notifications
CREATE TABLE kb.notification (
  notification_id uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         text        NOT NULL,
  kind            text        NOT NULL,
  message         text        NOT NULL,
  ref             jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  read_at         timestamptz
);
CREATE INDEX notification_user ON kb.notification (user_id, created_at DESC);

-- ---------------------------------------------------------------------------- upload registration
-- P3: called by the gateway as the uploader after the file is streamed to the raw store.
-- Space membership (caller's groups vs the space ACL) is checked by the gateway against the live
-- IdentityProvider (DECISIONS D-022); everything else is checked here.
CREATE OR REPLACE FUNCTION kb.register_upload(
  p_space text, p_filename text, p_sha256 text, p_size bigint, p_raw_uri text,
  p_source_ip inet, p_as_new_version_of text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me    text := kb.current_actor();
  s     kb.kb_source;
  rev   kb.source_revision;
  ext   text := lower(substring(p_filename from '\.([^.]+)$'));
  v_doc text;
  v_no  int;
  v_dup text;
  v_own text;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot register uploads' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO s FROM kb.kb_source WHERE source_id = p_space;
  IF s.source_id IS NULL OR s.source_type <> 'UPLOAD' THEN
    RAISE EXCEPTION 'unknown upload space %', p_space USING ERRCODE = 'P0002';
  END IF;
  IF s.status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'upload space % is not ACTIVE', p_space;
  END IF;
  SELECT * INTO rev FROM kb.source_revision WHERE revision_id = s.current_revision_id;
  IF p_size > rev.max_file_bytes THEN
    RAISE EXCEPTION 'file is % bytes; the space allows %', p_size, rev.max_file_bytes USING ERRCODE = '42501';
  END IF;
  IF ext IS NULL OR NOT (ext = ANY (rev.allowed_extensions)) THEN
    RAISE EXCEPTION 'file type .% is not allowed in %', ext, p_space USING ERRCODE = '42501';
  END IF;

  -- own duplicate (any of my live uploads in this BU, current version): refuse, point at it
  SELECT d.doc_id INTO v_own FROM kb.document d
    JOIN kb.kb_source ks ON ks.source_id = d.source_id
    JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current
   WHERE ks.bu = s.bu AND d.origin = 'UPLOAD' AND d.uploaded_by = me AND d.status = 'ACTIVE'
     AND v.sha256 = p_sha256
     AND d.doc_id IS DISTINCT FROM p_as_new_version_of
   LIMIT 1;
  IF v_own IS NOT NULL THEN
    RAISE EXCEPTION 'you already uploaded this file as document %', v_own USING ERRCODE = '23505';
  END IF;

  IF p_as_new_version_of IS NOT NULL THEN
    SELECT d.doc_id INTO v_doc FROM kb.document d
     WHERE d.doc_id = p_as_new_version_of AND d.origin = 'UPLOAD' AND d.uploaded_by = me
       AND d.status = 'ACTIVE' AND d.source_id = p_space
     FOR UPDATE;
    IF v_doc IS NULL THEN
      RAISE EXCEPTION 'document % is not one of your active uploads in %', p_as_new_version_of, p_space
        USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM kb.document_version WHERE doc_id = v_doc AND sha256 = p_sha256
                AND status NOT IN ('DELETED', 'PURGED')) THEN
      RAISE EXCEPTION 'this file is identical to an existing version of %', v_doc USING ERRCODE = '23505';
    END IF;
    SELECT coalesce(max(version_no), 0) + 1 INTO v_no FROM kb.document_version WHERE doc_id = v_doc;
  ELSE
    -- duplicate of a SharePoint document or a PUBLISHED upload in the same BU: link, do not index twice.
    -- Other users' PRIVATE uploads are never matched (no existence leak).
    SELECT d.doc_id INTO v_dup FROM kb.document d
      JOIN kb.kb_source ks ON ks.source_id = d.source_id
      JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current
     WHERE ks.bu = s.bu AND d.status = 'ACTIVE' AND v.sha256 = p_sha256
       AND (d.origin = 'SHAREPOINT' OR d.visibility = 'PUBLISHED')
     LIMIT 1;
    v_doc := 'up-' || replace(gen_random_uuid()::text, '-', '');
    v_no := 1;
    INSERT INTO kb.document (doc_id, source_id, external_item_id, current_path, title, origin, uploaded_by,
                             visibility, duplicate_of_doc_id)
    VALUES (v_doc, p_space, v_doc, p_filename, p_filename, 'UPLOAD', me, 'PRIVATE', v_dup);
  END IF;

  INSERT INTO kb.document_version (version_id, doc_id, version_no, sha256, size_bytes, raw_uri, status,
                                   uploaded_by, source_ip, original_filename, is_current)
  VALUES (v_doc || ':v' || v_no, v_doc, v_no, p_sha256, p_size, p_raw_uri,
          CASE WHEN v_dup IS NOT NULL THEN 'LINKED' ELSE 'RECEIVED' END,
          me, p_source_ip, p_filename, v_dup IS NOT NULL);
  IF v_dup IS NOT NULL THEN
    UPDATE kb.document SET current_version_no = v_no WHERE doc_id = v_doc;
  END IF;
  RETURN jsonb_build_object('doc_id', v_doc, 'version_id', v_doc || ':v' || v_no, 'version_no', v_no,
                            'duplicate_of', v_dup, 'status', CASE WHEN v_dup IS NOT NULL THEN 'LINKED'
                                                                  ELSE 'RECEIVED' END);
END $$;

-- ---------------------------------------------------------------------------- privileges
GRANT SELECT ON kb.retention_class, kb.notification TO kb_app, kb_pipeline, kb_auditor;
GRANT UPDATE (read_at) ON kb.notification TO kb_app;
GRANT INSERT ON kb.notification TO kb_pipeline;
GRANT UPDATE (reprocessed_at, reprocess_run_id) ON kb.exception_item TO kb_pipeline;
GRANT EXECUTE ON FUNCTION kb.register_upload(text, text, text, bigint, text, inet, text) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.retention_until(text, timestamptz), kb.releasable_reason(text)
  TO kb_app, kb_pipeline;
