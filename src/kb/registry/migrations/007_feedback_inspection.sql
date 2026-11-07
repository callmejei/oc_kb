-- 007_feedback_inspection: answer feedback, inspector labels, re-parse queue, exclude / include (Phase 6).
--   F1 only the asking user can flag an answer; the cited document versions are copied from that user's
--      own QUERY audit row, never taken from the client; one flag per query
--   F2 a flag is split per BU of the cited versions; makers/checkers of a BU see only their BU's part;
--      private uploads are never part of a flag target
--   F3 the question text is never copied into a flag (it stays in kb.query_log, auditor-only)
--   I1 inspector labels: MAKER/CHECKER of the document's BU; append-only; audited
--   R1 re-parse: MAKER of the document's BU; queued here, executed by the pipeline (never by the
--      gateway); never on a private upload; at most one queued per document; options allow-listed
--   X1 exclude restricts access: single actor (MAKER/CHECKER of the BU), recorded as an EXECUTED request
--   X2 a document leaves EXCLUDED only through an approved INCLUDE_DOCUMENT (four eyes); a trigger refuses
--      every other path, including the pipeline role

-- ---------------------------------------------------------------------------- shared vocabulary
CREATE OR REPLACE FUNCTION kb.problem_labels() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY['WRONG_ANSWER', 'INCOMPLETE', 'WRONG_CITATION', 'OUTDATED', 'TABLE_BROKEN', 'OCR_ERROR',
               'READING_ORDER', 'MISSING_TEXT', 'WRONG_HEADING', 'OTHER']
$$;

-- ---------------------------------------------------------------------------- F1-F3 answer feedback
CREATE TABLE kb.answer_feedback (
  feedback_id  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  query_id     uuid        NOT NULL UNIQUE,
  user_id      text        NOT NULL,
  rating       text        NOT NULL CHECK (rating IN ('UP', 'DOWN')),
  problem      text        CHECK (problem = ANY (kb.problem_labels())),
  reason       text        CHECK (length(reason) <= 2000),
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (rating = 'UP' OR (problem IS NOT NULL AND coalesce(reason, '') <> ''))
);
CREATE TRIGGER answer_feedback_append_only BEFORE UPDATE OR DELETE ON kb.answer_feedback
  FOR EACH ROW EXECUTE FUNCTION kb.tg_audit_append_only();
CREATE TRIGGER audit_answer_feedback AFTER INSERT ON kb.answer_feedback
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('feedback_id');

CREATE TABLE kb.feedback_target (
  feedback_id  uuid        NOT NULL REFERENCES kb.answer_feedback(feedback_id),
  bu           text        NOT NULL,
  version_ids  text[]      NOT NULL,
  status       text        NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
  resolved_by  text,
  resolved_at  timestamptz,
  resolution   text,
  PRIMARY KEY (feedback_id, bu),
  CHECK ((status = 'OPEN') = (resolved_by IS NULL AND resolved_at IS NULL))
);

CREATE OR REPLACE FUNCTION kb.tg_feedback_target_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'feedback cannot be deleted' USING ERRCODE = '42501';
  END IF;
  IF current_user <> kb.owner_role() THEN
    RAISE EXCEPTION 'feedback changes only through kb.resolve_feedback' USING ERRCODE = '42501';
  END IF;
  IF OLD.status <> 'OPEN' THEN
    RAISE EXCEPTION 'feedback % for % already %', OLD.feedback_id, OLD.bu, OLD.status USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER feedback_target_guard BEFORE UPDATE OR DELETE ON kb.feedback_target
  FOR EACH ROW EXECUTE FUNCTION kb.tg_feedback_target_guard();
CREATE TRIGGER audit_feedback_target AFTER UPDATE ON kb.feedback_target
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('feedback_id');

CREATE OR REPLACE FUNCTION kb.submit_feedback(p_query_id uuid, p_rating text, p_problem text, p_reason text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); a kb.audit_log; v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot give feedback' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO a FROM kb.audit_log
   WHERE action = 'QUERY' AND object_type = 'query' AND object_id = p_query_id::text;
  IF a.audit_id IS NULL OR a.actor_id IS DISTINCT FROM me THEN   -- someone else's query looks unknown (F1)
    RAISE EXCEPTION 'unknown query %', p_query_id USING ERRCODE = 'P0002';
  END IF;
  INSERT INTO kb.answer_feedback (query_id, user_id, rating, problem, reason)
  VALUES (p_query_id, me, p_rating, p_problem, nullif(p_reason, ''))
  RETURNING feedback_id INTO v_id;
  IF p_rating = 'DOWN' THEN
    INSERT INTO kb.feedback_target (feedback_id, bu, version_ids)
    SELECT v_id, s.bu, array_agg(DISTINCT v.version_id ORDER BY v.version_id)
      FROM jsonb_array_elements_text(coalesce(a.details -> 'cited_doc_versions', '[]'::jsonb)) AS c(version_id)
      JOIN kb.document_version v ON v.version_id = c.version_id
      JOIN kb.document d ON d.doc_id = v.doc_id
      JOIN kb.kb_source s ON s.source_id = d.source_id
     WHERE NOT (d.origin = 'UPLOAD' AND d.visibility IS DISTINCT FROM 'PUBLISHED')   -- F2
     GROUP BY s.bu;
  END IF;
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION kb.resolve_feedback(p_feedback_id uuid, p_bu text, p_resolution text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); t kb.feedback_target;
BEGIN
  IF NOT (kb.has_gov_role(me, p_bu, 'MAKER') OR kb.has_gov_role(me, p_bu, 'CHECKER')) THEN
    RAISE EXCEPTION '% is not a MAKER or CHECKER for BU %', me, p_bu USING ERRCODE = '42501';
  END IF;
  IF coalesce(p_resolution, '') = '' THEN RAISE EXCEPTION 'a resolution note is required'; END IF;
  SELECT * INTO t FROM kb.feedback_target WHERE feedback_id = p_feedback_id AND bu = p_bu FOR UPDATE;
  IF t.feedback_id IS NULL THEN
    RAISE EXCEPTION 'unknown feedback % for %', p_feedback_id, p_bu USING ERRCODE = 'P0002';
  END IF;
  UPDATE kb.feedback_target SET status = 'RESOLVED', resolved_by = me, resolved_at = clock_timestamp(),
         resolution = p_resolution
   WHERE feedback_id = p_feedback_id AND bu = p_bu;
END $$;

-- ---------------------------------------------------------------------------- helpers
-- BU of a document, and whether it is a private upload (governance roles never act on those)
CREATE OR REPLACE FUNCTION kb.doc_governance_bu(p_doc_id text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT s.bu FROM kb.document d JOIN kb.kb_source s USING (source_id)
   WHERE d.doc_id = p_doc_id AND NOT (d.origin = 'UPLOAD' AND d.visibility IS DISTINCT FROM 'PUBLISHED')
$$;

-- ---------------------------------------------------------------------------- I1 inspector labels
CREATE TABLE kb.inspection_label (
  label_id     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id       text        NOT NULL REFERENCES kb.document(doc_id),
  version_id   text        NOT NULL REFERENCES kb.document_version(version_id),
  page         int         CHECK (page > 0),
  item_ref     text        CHECK (length(item_ref) <= 200),
  problem      text        NOT NULL CHECK (problem = ANY (kb.problem_labels())),
  note         text        CHECK (length(note) <= 2000),
  feedback_id  uuid        REFERENCES kb.answer_feedback(feedback_id),
  created_by   text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER inspection_label_append_only BEFORE UPDATE OR DELETE ON kb.inspection_label
  FOR EACH ROW EXECUTE FUNCTION kb.tg_audit_append_only();
CREATE TRIGGER audit_inspection_label AFTER INSERT ON kb.inspection_label
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('label_id');

CREATE OR REPLACE FUNCTION kb.add_inspection_label(
  p_version_id text, p_page int, p_item_ref text, p_problem text, p_note text, p_feedback_id uuid DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); v_doc text; v_bu text; v_id uuid;
BEGIN
  SELECT doc_id INTO v_doc FROM kb.document_version WHERE version_id = p_version_id;
  v_bu := kb.doc_governance_bu(v_doc);
  IF v_doc IS NULL OR v_bu IS NULL THEN
    RAISE EXCEPTION 'unknown document version %', p_version_id USING ERRCODE = 'P0002';
  END IF;
  IF NOT (kb.has_gov_role(me, v_bu, 'MAKER') OR kb.has_gov_role(me, v_bu, 'CHECKER')) THEN
    RAISE EXCEPTION '% is not a MAKER or CHECKER for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;
  INSERT INTO kb.inspection_label (doc_id, version_id, page, item_ref, problem, note, feedback_id, created_by)
  VALUES (v_doc, p_version_id, p_page, p_item_ref, p_problem, p_note, p_feedback_id, me)
  RETURNING label_id INTO v_id;
  RETURN v_id;
END $$;

-- ---------------------------------------------------------------------------- R1 re-parse queue
CREATE TABLE kb.reparse_request (
  reparse_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id            text        NOT NULL REFERENCES kb.document(doc_id),
  from_version_id   text        NOT NULL REFERENCES kb.document_version(version_id),
  options           jsonb       NOT NULL,
  reason            text        NOT NULL CHECK (reason <> '' AND length(reason) <= 2000),
  feedback_id       uuid        REFERENCES kb.answer_feedback(feedback_id),
  requested_by      text        NOT NULL,
  status            text        NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'DONE', 'FAILED')),
  result_version_id text        REFERENCES kb.document_version(version_id),
  error             text,
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at       timestamptz,
  CHECK ((status = 'QUEUED') = (finished_at IS NULL)),
  CHECK (status <> 'DONE' OR result_version_id IS NOT NULL)
);
CREATE UNIQUE INDEX reparse_one_queued ON kb.reparse_request (doc_id) WHERE status = 'QUEUED';

CREATE OR REPLACE FUNCTION kb.tg_reparse_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 're-parse requests cannot be deleted' USING ERRCODE = '42501';
  END IF;
  IF OLD.status <> 'QUEUED' THEN
    RAISE EXCEPTION 're-parse % already %', OLD.reparse_id, OLD.status USING ERRCODE = '42501';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['status', 'result_version_id', 'error', 'finished_at'])
     <> (to_jsonb(OLD) - ARRAY['status', 'result_version_id', 'error', 'finished_at']) THEN
    RAISE EXCEPTION 'only the outcome of a re-parse may change' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER reparse_guard BEFORE UPDATE OR DELETE ON kb.reparse_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_reparse_guard();
CREATE TRIGGER audit_reparse AFTER INSERT OR UPDATE ON kb.reparse_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('reparse_id');

CREATE OR REPLACE FUNCTION kb.request_reparse(p_doc_id text, p_options jsonb, p_reason text,
                                              p_feedback_id uuid DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); d kb.document; v_bu text; v_ver text; v_id uuid; k text;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot request a re-parse' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO d FROM kb.document WHERE doc_id = p_doc_id FOR UPDATE;
  v_bu := kb.doc_governance_bu(p_doc_id);
  IF d.doc_id IS NULL OR v_bu IS NULL THEN
    RAISE EXCEPTION 'unknown document %', p_doc_id USING ERRCODE = 'P0002';
  END IF;
  IF NOT kb.has_gov_role(me, v_bu, 'MAKER') THEN
    RAISE EXCEPTION '% is not a MAKER for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;
  IF d.status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'document % is %; only ACTIVE documents can be re-parsed', p_doc_id, d.status;
  END IF;
  SELECT version_id INTO v_ver FROM kb.document_version WHERE doc_id = p_doc_id AND is_current AND status = 'INDEXED';
  IF v_ver IS NULL THEN
    RAISE EXCEPTION 'document % has no indexed current version', p_doc_id;
  END IF;
  IF coalesce(p_reason, '') = '' THEN RAISE EXCEPTION 'a reason is required to re-parse'; END IF;
  IF jsonb_typeof(p_options) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'options must be an object'; END IF;
  FOR k IN SELECT jsonb_object_keys(p_options) LOOP
    IF k NOT IN ('ocr_mode', 'table_mode', 'table_cell_matching') THEN
      RAISE EXCEPTION 'unknown re-parse option %', k;
    END IF;
  END LOOP;
  IF coalesce(p_options ->> 'ocr_mode', 'default') NOT IN ('default', 'full_page')
     OR coalesce(p_options ->> 'table_mode', 'accurate') NOT IN ('accurate', 'fast')
     OR coalesce(p_options ->> 'table_cell_matching', 'true') NOT IN ('true', 'false') THEN
    RAISE EXCEPTION 'invalid re-parse option value in %', p_options;
  END IF;
  INSERT INTO kb.reparse_request (doc_id, from_version_id, options, reason, feedback_id, requested_by)
  VALUES (p_doc_id, v_ver, p_options, p_reason, p_feedback_id, me)
  RETURNING reparse_id INTO v_id;
  RETURN v_id;
END $$;

-- ---------------------------------------------------------------------------- X1/X2 exclude / include
ALTER TABLE kb.document ADD COLUMN restore_pending boolean NOT NULL DEFAULT false;

ALTER TABLE kb.approval_request DROP CONSTRAINT approval_request_request_type_check;
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_request_type_check CHECK (request_type IN (
  'ONBOARD_SOURCE', 'CHANGE_SOURCE', 'REACTIVATE_SOURCE', 'RELEASE_EXCEPTION', 'RELEASE_LEGAL_HOLD',
  'PUBLISH_DOCUMENT', 'UNPUBLISH_DOCUMENT', 'EXCLUDE_DOCUMENT', 'INCLUDE_DOCUMENT'));
ALTER TABLE kb.approval_request DROP CONSTRAINT approval_request_executed_shape;
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_executed_shape
  CHECK ((status = 'EXECUTED') = (request_type IN ('UNPUBLISH_DOCUMENT', 'EXCLUDE_DOCUMENT')));
ALTER TABLE kb.approval_request DROP CONSTRAINT approval_request_target_shape;
ALTER TABLE kb.approval_request ADD CONSTRAINT approval_request_target_shape CHECK (
     (request_type IN ('ONBOARD_SOURCE','CHANGE_SOURCE') AND revision_id  IS NOT NULL)
  OR (request_type = 'REACTIVATE_SOURCE')
  OR (request_type = 'RELEASE_EXCEPTION'  AND exception_id IS NOT NULL)
  OR (request_type = 'RELEASE_LEGAL_HOLD' AND hold_id      IS NOT NULL)
  OR (request_type IN ('PUBLISH_DOCUMENT','UNPUBLISH_DOCUMENT','EXCLUDE_DOCUMENT','INCLUDE_DOCUMENT')
      AND doc_id IS NOT NULL));
CREATE UNIQUE INDEX approval_request_one_pending_include
  ON kb.approval_request (doc_id) WHERE status = 'PENDING' AND request_type = 'INCLUDE_DOCUMENT';

-- X2: EXCLUDED is entered and left only inside the governance functions (owner role)
CREATE OR REPLACE FUNCTION kb.tg_document_exclusion_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> kb.owner_role() THEN
    IF (OLD.status = 'EXCLUDED') <> (NEW.status = 'EXCLUDED') THEN
      RAISE EXCEPTION 'document % enters or leaves EXCLUDED only through exclude / approved include',
        NEW.doc_id USING ERRCODE = '42501';
    END IF;
    IF NEW.restore_pending AND NOT OLD.restore_pending THEN
      RAISE EXCEPTION 'restore_pending is set only by an approved include' USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER document_exclusion_guard BEFORE UPDATE ON kb.document
  FOR EACH ROW EXECUTE FUNCTION kb.tg_document_exclusion_guard();

CREATE OR REPLACE FUNCTION kb.exclude_document(p_doc_id text, p_reason text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); d kb.document; v_bu text; v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot exclude documents' USING ERRCODE = '42501';
  END IF;
  IF coalesce(p_reason, '') = '' THEN RAISE EXCEPTION 'a reason is required to exclude'; END IF;
  SELECT * INTO d FROM kb.document WHERE doc_id = p_doc_id FOR UPDATE;
  v_bu := kb.doc_governance_bu(p_doc_id);
  IF d.doc_id IS NULL OR v_bu IS NULL THEN
    RAISE EXCEPTION 'unknown document %', p_doc_id USING ERRCODE = 'P0002';
  END IF;
  IF NOT (kb.has_gov_role(me, v_bu, 'MAKER') OR kb.has_gov_role(me, v_bu, 'CHECKER')) THEN
    RAISE EXCEPTION '% may not exclude %', me, p_doc_id USING ERRCODE = '42501';
  END IF;
  IF d.status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'document % is %; only ACTIVE documents can be excluded', p_doc_id, d.status;
  END IF;
  UPDATE kb.document SET status = 'EXCLUDED', deleted_at = clock_timestamp(), restore_pending = false
   WHERE doc_id = p_doc_id;
  UPDATE kb.reparse_request SET status = 'FAILED', error = 'document excluded', finished_at = clock_timestamp()
   WHERE doc_id = p_doc_id AND status = 'QUEUED';
  INSERT INTO kb.approval_request (request_type, source_id, doc_id, maker_id, maker_comment,
                                   status, decision_reason, decided_at)
  VALUES ('EXCLUDE_DOCUMENT', d.source_id, p_doc_id, me, p_reason, 'EXECUTED', p_reason, clock_timestamp())
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION kb.request_include(p_doc_id text, p_comment text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); d kb.document; v_bu text; v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot submit governance requests' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO d FROM kb.document WHERE doc_id = p_doc_id FOR UPDATE;
  v_bu := kb.doc_governance_bu(p_doc_id);
  IF d.doc_id IS NULL OR v_bu IS NULL THEN
    RAISE EXCEPTION 'unknown document %', p_doc_id USING ERRCODE = 'P0002';
  END IF;
  IF NOT kb.has_gov_role(me, v_bu, 'MAKER') THEN
    RAISE EXCEPTION '% is not a MAKER for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;
  IF d.status <> 'EXCLUDED' THEN
    RAISE EXCEPTION 'document % is not EXCLUDED', p_doc_id;
  END IF;
  INSERT INTO kb.approval_request (request_type, source_id, doc_id, maker_id, maker_comment)
  VALUES ('INCLUDE_DOCUMENT', d.source_id, p_doc_id, me, p_comment)
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

-- decide_request: 002 behaviour + INCLUDE_DOCUMENT
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
  ELSIF r.request_type = 'INCLUDE_DOCUMENT' AND p_approve THEN
    SELECT * INTO v_doc FROM kb.document WHERE doc_id = r.doc_id FOR UPDATE;
    IF v_doc.status <> 'EXCLUDED' THEN
      RAISE EXCEPTION 'document % is no longer EXCLUDED', r.doc_id;
    END IF;
    UPDATE kb.document SET status = 'ACTIVE', deleted_at = NULL, restore_pending = true WHERE doc_id = r.doc_id;
  END IF;
END $$;

-- ---------------------------------------------------------------------------- privileges
REVOKE ALL ON kb.answer_feedback, kb.feedback_target, kb.inspection_label, kb.reparse_request FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.submit_feedback(uuid, text, text, text), kb.resolve_feedback(uuid, text, text),
  kb.add_inspection_label(text, int, text, text, text, uuid), kb.request_reparse(text, jsonb, text, uuid),
  kb.exclude_document(text, text), kb.request_include(text, text) FROM PUBLIC;
GRANT SELECT ON kb.answer_feedback, kb.feedback_target, kb.inspection_label, kb.reparse_request
  TO kb_app, kb_auditor;
GRANT SELECT ON kb.reparse_request, kb.inspection_label TO kb_pipeline;
GRANT UPDATE (status, result_version_id, error, finished_at) ON kb.reparse_request TO kb_pipeline;
GRANT EXECUTE ON FUNCTION kb.submit_feedback(uuid, text, text, text), kb.resolve_feedback(uuid, text, text),
  kb.add_inspection_label(text, int, text, text, text, uuid), kb.request_reparse(text, jsonb, text, uuid),
  kb.exclude_document(text, text), kb.request_include(text, text) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.problem_labels(), kb.doc_governance_bu(text) TO kb_app, kb_pipeline;
-- deliberately NOT granted: kb_pipeline cannot exclude, include, request re-parse or touch feedback
