-- 002_uploads: manual uploads as a second document source (CLAUDE.md section 5).
-- Extends 001; every 001 guarantee (G1-G10) is kept. New guarantees:
--   U1 an upload space is a source of type UPLOAD, onboarded through the same four-eyes flow
--   U2 only the uploader can request PUBLISH_DOCUMENT; only a CHECKER of the space's BU, never the
--      uploader, decides it
--   U3 visibility changes only inside governance functions; uploads are inserted PRIVATE
--   U4 UNPUBLISH_DOCUMENT is single-actor (it restricts access) and recorded as an EXECUTED request
--   U5 upload receipt (uploader, time, source IP, hash) and every publish/unpublish are audited

-- ---------------------------------------------------------------------------- sources
ALTER TABLE kb.kb_source
  ADD COLUMN source_type text NOT NULL DEFAULT 'SHAREPOINT' CHECK (source_type IN ('SHAREPOINT', 'UPLOAD')),
  ALTER COLUMN sp_site_id DROP NOT NULL,
  ALTER COLUMN sp_drive_id DROP NOT NULL,
  ALTER COLUMN sp_folder_path DROP NOT NULL;
ALTER TABLE kb.kb_source ADD CONSTRAINT kb_source_type_shape CHECK (
     (source_type = 'SHAREPOINT' AND sp_site_id IS NOT NULL AND sp_drive_id IS NOT NULL AND sp_folder_path IS NOT NULL)
  OR (source_type = 'UPLOAD'     AND sp_site_id IS NULL     AND sp_drive_id IS NULL     AND sp_folder_path IS NULL));

CREATE OR REPLACE FUNCTION kb.tg_source_type_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source_type <> OLD.source_type OR NEW.bu <> OLD.bu THEN
    RAISE EXCEPTION 'source_type and bu of a source are immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER kb_source_type_immutable BEFORE UPDATE ON kb.kb_source
  FOR EACH ROW EXECUTE FUNCTION kb.tg_source_type_immutable();

-- ---------------------------------------------------------------------------- documents
ALTER TABLE kb.document RENAME COLUMN sp_item_id TO external_item_id;
ALTER TABLE kb.document
  ADD COLUMN origin text NOT NULL DEFAULT 'SHAREPOINT' CHECK (origin IN ('SHAREPOINT', 'UPLOAD')),
  ADD COLUMN uploaded_by text,
  ADD COLUMN visibility text CHECK (visibility IN ('PRIVATE', 'PUBLISHED')),
  ADD COLUMN duplicate_of_doc_id text REFERENCES kb.document(doc_id);
ALTER TABLE kb.document ADD CONSTRAINT document_origin_shape CHECK (
     (origin = 'SHAREPOINT' AND uploaded_by IS NULL     AND visibility IS NULL)
  OR (origin = 'UPLOAD'     AND uploaded_by IS NOT NULL AND visibility IS NOT NULL));

ALTER TABLE kb.document_version
  ADD COLUMN uploaded_by text,
  ADD COLUMN source_ip inet,
  ADD COLUMN original_filename text;

CREATE OR REPLACE FUNCTION kb.tg_document_upload_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE st text;
BEGIN
  SELECT source_type INTO st FROM kb.kb_source WHERE source_id = NEW.source_id;
  IF st IS DISTINCT FROM NEW.origin THEN
    RAISE EXCEPTION 'document origin % does not match source type %', NEW.origin, st USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.origin = 'UPLOAD' AND NEW.visibility <> 'PRIVATE' THEN
      RAISE EXCEPTION 'uploads start PRIVATE' USING ERRCODE = '42501';
    END IF;
  ELSE
    IF NEW.origin <> OLD.origin OR NEW.uploaded_by IS DISTINCT FROM OLD.uploaded_by
       OR NEW.source_id <> OLD.source_id THEN
      RAISE EXCEPTION 'origin, uploader and source of a document are immutable' USING ERRCODE = '42501';
    END IF;
    IF NEW.visibility IS DISTINCT FROM OLD.visibility AND current_user <> kb.owner_role() THEN
      RAISE EXCEPTION 'visibility changes only through publish/unpublish' USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER document_upload_guard BEFORE INSERT OR UPDATE ON kb.document
  FOR EACH ROW EXECUTE FUNCTION kb.tg_document_upload_guard();

-- U5: audit visibility / status changes of uploads and every upload receipt
CREATE OR REPLACE FUNCTION kb.tg_upload_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
BEGIN
  IF TG_TABLE_NAME = 'document' THEN
    IF NEW.origin = 'UPLOAD' AND (NEW.visibility IS DISTINCT FROM OLD.visibility OR NEW.status <> OLD.status) THEN
      INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, before_state, after_state)
      VALUES (kb.current_actor(), 'UPDATE', 'document', NEW.doc_id,
              jsonb_build_object('visibility', OLD.visibility, 'status', OLD.status),
              jsonb_build_object('visibility', NEW.visibility, 'status', NEW.status));
    END IF;
  ELSIF NEW.uploaded_by IS NOT NULL THEN       -- document_version insert of an upload
    INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, details)
    VALUES (kb.current_actor(), 'UPLOAD_RECEIVED', 'document_version', NEW.version_id,
            jsonb_build_object('uploaded_by', NEW.uploaded_by, 'source_ip', host(NEW.source_ip),
                               'sha256', NEW.sha256, 'size_bytes', NEW.size_bytes,
                               'original_filename', NEW.original_filename, 'received_at', NEW.created_at));
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER audit_document_upload AFTER UPDATE ON kb.document
  FOR EACH ROW EXECUTE FUNCTION kb.tg_upload_audit();
CREATE TRIGGER audit_upload_received AFTER INSERT ON kb.document_version
  FOR EACH ROW EXECUTE FUNCTION kb.tg_upload_audit();

-- ---------------------------------------------------------------------------- approval requests
ALTER TABLE kb.approval_request ADD COLUMN doc_id text REFERENCES kb.document(doc_id);

ALTER TABLE kb.approval_request DROP CONSTRAINT approval_request_request_type_check;
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_request_type_check CHECK (request_type IN (
  'ONBOARD_SOURCE', 'CHANGE_SOURCE', 'REACTIVATE_SOURCE', 'RELEASE_EXCEPTION', 'RELEASE_LEGAL_HOLD',
  'PUBLISH_DOCUMENT', 'UNPUBLISH_DOCUMENT'));

ALTER TABLE kb.approval_request DROP CONSTRAINT approval_request_status_check;
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_status_check
  CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'EXECUTED'));

ALTER TABLE kb.approval_request DROP CONSTRAINT approval_request_decision_shape;
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_decision_shape CHECK (
     (status = 'PENDING'                AND checker_id IS NULL     AND decided_at IS NULL)
  OR (status = 'WITHDRAWN'              AND checker_id IS NULL     AND decided_at IS NOT NULL)
  OR (status IN ('APPROVED','REJECTED') AND checker_id IS NOT NULL AND decided_at IS NOT NULL)
  OR (status = 'EXECUTED'               AND checker_id IS NULL     AND decided_at IS NOT NULL));
-- U4: EXECUTED exists only for the single-actor, access-restricting type, and that type is only EXECUTED
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_executed_shape
  CHECK ((status = 'EXECUTED') = (request_type = 'UNPUBLISH_DOCUMENT'));

ALTER TABLE kb.approval_request DROP CONSTRAINT approval_request_target_shape;
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_target_shape CHECK (
     (request_type IN ('ONBOARD_SOURCE','CHANGE_SOURCE') AND revision_id  IS NOT NULL)
  OR (request_type = 'REACTIVATE_SOURCE')
  OR (request_type = 'RELEASE_EXCEPTION'  AND exception_id IS NOT NULL)
  OR (request_type = 'RELEASE_LEGAL_HOLD' AND hold_id      IS NOT NULL)
  OR (request_type IN ('PUBLISH_DOCUMENT','UNPUBLISH_DOCUMENT') AND doc_id IS NOT NULL));

CREATE UNIQUE INDEX approval_request_one_pending_publish
  ON kb.approval_request (doc_id) WHERE status = 'PENDING' AND request_type = 'PUBLISH_DOCUMENT';

-- ---------------------------------------------------------------------------- functions
-- U2: the uploader asks for a specific document to be published to the space's groups
CREATE OR REPLACE FUNCTION kb.request_publish(p_doc_id text, p_comment text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor();
  d kb.document;
  s kb.kb_source;
  v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot submit governance requests' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO d FROM kb.document WHERE doc_id = p_doc_id FOR UPDATE;
  IF d.doc_id IS NULL THEN RAISE EXCEPTION 'unknown document %', p_doc_id USING ERRCODE = 'P0002'; END IF;
  IF d.origin <> 'UPLOAD' OR d.uploaded_by <> me THEN
    RAISE EXCEPTION 'only the uploader can request publication of %', p_doc_id USING ERRCODE = '42501';
  END IF;
  IF d.status <> 'ACTIVE' OR d.visibility <> 'PRIVATE' THEN
    RAISE EXCEPTION 'document % is % / %; only ACTIVE PRIVATE uploads can be published',
      p_doc_id, d.status, d.visibility;
  END IF;
  SELECT * INTO s FROM kb.kb_source WHERE source_id = d.source_id;
  IF s.status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'upload space % is not ACTIVE', s.source_id;
  END IF;
  INSERT INTO kb.approval_request (request_type, source_id, doc_id, maker_id, maker_comment)
  VALUES ('PUBLISH_DOCUMENT', d.source_id, p_doc_id, me, p_comment)
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

-- U4: unpublishing restricts access, so one actor suffices: the uploader or a governance role of the BU
CREATE OR REPLACE FUNCTION kb.unpublish_document(p_doc_id text, p_reason text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor();
  d kb.document;
  v_bu text;
  v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot unpublish' USING ERRCODE = '42501';
  END IF;
  IF coalesce(p_reason, '') = '' THEN RAISE EXCEPTION 'a reason is required to unpublish'; END IF;
  SELECT * INTO d FROM kb.document WHERE doc_id = p_doc_id FOR UPDATE;
  IF d.doc_id IS NULL THEN RAISE EXCEPTION 'unknown document %', p_doc_id USING ERRCODE = 'P0002'; END IF;
  SELECT bu INTO v_bu FROM kb.kb_source WHERE source_id = d.source_id;
  IF NOT (d.uploaded_by = me OR kb.has_gov_role(me, v_bu, 'MAKER') OR kb.has_gov_role(me, v_bu, 'CHECKER')) THEN
    RAISE EXCEPTION '% may not unpublish %', me, p_doc_id USING ERRCODE = '42501';
  END IF;
  IF d.origin <> 'UPLOAD' OR d.visibility <> 'PUBLISHED' THEN
    RAISE EXCEPTION 'document % is not a published upload', p_doc_id;
  END IF;
  UPDATE kb.document SET visibility = 'PRIVATE' WHERE doc_id = p_doc_id;
  INSERT INTO kb.approval_request (request_type, source_id, doc_id, maker_id, maker_comment,
                                   status, decision_reason, decided_at)
  VALUES ('UNPUBLISH_DOCUMENT', d.source_id, p_doc_id, me, p_reason, 'EXECUTED', p_reason, clock_timestamp())
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

-- the uploader deletes their own upload: out of search immediately; raw copy kept until retention
CREATE OR REPLACE FUNCTION kb.delete_upload(p_doc_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); d kb.document;
BEGIN
  SELECT * INTO d FROM kb.document WHERE doc_id = p_doc_id FOR UPDATE;
  IF d.doc_id IS NULL THEN RAISE EXCEPTION 'unknown document %', p_doc_id USING ERRCODE = 'P0002'; END IF;
  IF d.origin <> 'UPLOAD' OR d.uploaded_by <> me THEN
    RAISE EXCEPTION 'only the uploader can delete %', p_doc_id USING ERRCODE = '42501';
  END IF;
  UPDATE kb.document SET status = 'DELETED', deleted_at = clock_timestamp()
   WHERE doc_id = p_doc_id AND status = 'ACTIVE';
  UPDATE kb.approval_request SET status = 'WITHDRAWN', decided_at = clock_timestamp(),
         decision_reason = 'document deleted by uploader'
   WHERE doc_id = p_doc_id AND status = 'PENDING';
END $$;

-- decide_request: 001 behaviour + PUBLISH_DOCUMENT
CREATE OR REPLACE FUNCTION kb.decide_request(p_request_id uuid, p_approve boolean, p_reason text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor();
  r kb.approval_request;
  v_bu text;
  v_prev uuid;
  v_doc kb.document;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot decide approval requests' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO r FROM kb.approval_request WHERE request_id = p_request_id FOR UPDATE;
  IF r.request_id IS NULL THEN RAISE EXCEPTION 'unknown request %', p_request_id USING ERRCODE = 'P0002'; END IF;
  SELECT bu INTO v_bu FROM kb.kb_source WHERE source_id = r.source_id;
  IF NOT kb.has_gov_role(me, v_bu, 'CHECKER') THEN
    RAISE EXCEPTION '% is not a CHECKER for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;
  IF me = r.maker_id THEN
    RAISE EXCEPTION 'maker cannot check their own request' USING ERRCODE = '42501';
  END IF;

  UPDATE kb.approval_request
     SET status = CASE WHEN p_approve THEN 'APPROVED' ELSE 'REJECTED' END,
         checker_id = me, decision_reason = p_reason, decided_at = clock_timestamp()
   WHERE request_id = p_request_id;

  IF r.request_type IN ('ONBOARD_SOURCE','CHANGE_SOURCE') THEN
    IF p_approve THEN
      SELECT current_revision_id INTO v_prev FROM kb.kb_source WHERE source_id = r.source_id;
      UPDATE kb.source_revision SET status = 'APPROVED' WHERE revision_id = r.revision_id;
      IF v_prev IS NOT NULL AND v_prev <> r.revision_id THEN
        UPDATE kb.source_revision SET status = 'SUPERSEDED' WHERE revision_id = v_prev AND status = 'APPROVED';
      END IF;
      UPDATE kb.kb_source SET current_revision_id = r.revision_id, status = 'ACTIVE'
       WHERE source_id = r.source_id;
    ELSE
      UPDATE kb.source_revision SET status = 'REJECTED' WHERE revision_id = r.revision_id;
    END IF;
  ELSIF r.request_type = 'REACTIVATE_SOURCE' AND p_approve THEN
    UPDATE kb.kb_source SET status = 'ACTIVE' WHERE source_id = r.source_id;
  ELSIF r.request_type = 'RELEASE_EXCEPTION' AND p_approve THEN
    UPDATE kb.exception_item SET status = 'RELEASED', resolved_at = clock_timestamp()
     WHERE exception_id = r.exception_id AND status = 'OPEN';
  ELSIF r.request_type = 'RELEASE_LEGAL_HOLD' AND p_approve THEN
    UPDATE kb.legal_hold SET released_by = me, released_at = clock_timestamp()
     WHERE hold_id = r.hold_id AND released_at IS NULL;
  ELSIF r.request_type = 'PUBLISH_DOCUMENT' AND p_approve THEN
    SELECT * INTO v_doc FROM kb.document WHERE doc_id = r.doc_id FOR UPDATE;
    IF v_doc.status <> 'ACTIVE' OR v_doc.visibility <> 'PRIVATE' THEN
      RAISE EXCEPTION 'document % is no longer an ACTIVE PRIVATE upload', r.doc_id;
    END IF;
    UPDATE kb.document SET visibility = 'PUBLISHED' WHERE doc_id = r.doc_id;
  END IF;
END $$;

-- ---------------------------------------------------------------------------- privileges
REVOKE ALL ON FUNCTION kb.request_publish(text, text), kb.unpublish_document(text, text),
                       kb.delete_upload(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.request_publish(text, text), kb.unpublish_document(text, text),
                          kb.delete_upload(text) TO kb_app;
GRANT INSERT ON kb.audit_log TO kb_pipeline;
