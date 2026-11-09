-- 012_semantic_metrics: governed metric definitions (Phase 9c, D-067). A metric is a named, versioned measure on one
-- data product, compiled IN CODE to SQL and executed as the end user through the same policy rewrite as any query
-- (row filters, masks). The LLM never writes metric SQL.
--
-- Guarantees (database):
--   M1  maker <> checker; locked revisions; pipeline never governs (same machinery as data products)
--   M2  publishing or changing a metric needs a checker; retiring is single-actor (it only removes an answer path)
--   M3  a metric may reference only columns of its product that carry NO mask in the product's current policy;
--       checked at submit and again at approval (a mask added meanwhile blocks it). The policy engine re-checks per
--       user at run time, so a mask added after publication stops the metric for everyone it would mask.
--   M4  approval of a revision drafted against an older one is refused (stale)
--   M5  every change audited

CREATE OR REPLACE FUNCTION kb.valid_metric_def(d jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_typeof(d) = 'object'
     AND (SELECT count(*) FROM jsonb_object_keys(d) k
           WHERE k NOT IN ('label', 'synonyms', 'concept', 'table', 'measure', 'dimensions', 'unit', 'columns')) = 0
     AND length(d->>'label') BETWEEN 2 AND 80
     AND jsonb_typeof(d->'synonyms') = 'array' AND jsonb_array_length(d->'synonyms') <= 20
     AND (d->>'table') ~ '^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$'
     AND jsonb_typeof(d->'measure') = 'object'
     AND (d->'measure'->>'agg') IN ('sum', 'avg', 'min', 'max', 'count', 'count_distinct', 'ratio')
     AND jsonb_typeof(d->'dimensions') = 'array' AND jsonb_array_length(d->'dimensions') <= 5
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(d->'dimensions') x WHERE x !~ '^[a-z_][a-z0-9_]{0,62}$')
     AND (d->>'unit') IN ('%', 'EXB$', 'count', 'number')
     AND jsonb_typeof(d->'columns') = 'array' AND jsonb_array_length(d->'columns') BETWEEN 1 AND 20
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(d->'columns') x WHERE x !~ '^[a-z_][a-z0-9_]{0,62}$')
$$;

CREATE TABLE kb.metric (
  metric_id           text        PRIMARY KEY CHECK (metric_id ~ '^[a-z][a-z0-9_]{2,62}$'),
  product_id          text        NOT NULL REFERENCES kb.data_product(product_id),
  gov_bu              text        NOT NULL DEFAULT 'data',
  status              text        NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
  current_revision_id uuid,
  created_by          text        NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kb.metric_revision (
  revision_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  metric_id        text        NOT NULL REFERENCES kb.metric(metric_id),
  revision_no      int         NOT NULL CHECK (revision_no > 0),
  base_revision_id uuid        REFERENCES kb.metric_revision(revision_id),
  definition       jsonb       NOT NULL CHECK (kb.valid_metric_def(definition)),
  description      text        NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
  status           text        NOT NULL DEFAULT 'DRAFT'
                   CHECK (status IN ('DRAFT', 'SUBMITTED', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'SUPERSEDED')),
  created_by       text        NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  locked_at        timestamptz,
  UNIQUE (metric_id, revision_no),
  CHECK ((status = 'DRAFT') = (locked_at IS NULL))
);
ALTER TABLE kb.metric ADD CONSTRAINT metric_current_revision_fk
  FOREIGN KEY (current_revision_id) REFERENCES kb.metric_revision(revision_id);

CREATE TABLE kb.metric_request (
  request_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  request_type    text        NOT NULL CHECK (request_type IN ('PUBLISH_METRIC', 'RETIRE_METRIC')),
  metric_id       text        NOT NULL REFERENCES kb.metric(metric_id),
  revision_id     uuid        REFERENCES kb.metric_revision(revision_id),
  maker_id        text        NOT NULL,
  maker_comment   text,
  submitted_at    timestamptz NOT NULL DEFAULT now(),
  status          text        NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'EXECUTED')),
  checker_id      text,
  decision_reason text,
  decided_at      timestamptz,
  CONSTRAINT metric_request_four_eyes CHECK (checker_id IS NULL OR checker_id <> maker_id),
  CONSTRAINT metric_request_decision_shape CHECK (
       (status = 'PENDING'                AND checker_id IS NULL     AND decided_at IS NULL)
    OR (status = 'WITHDRAWN'              AND checker_id IS NULL     AND decided_at IS NOT NULL)
    OR (status IN ('APPROVED','REJECTED') AND checker_id IS NOT NULL AND decided_at IS NOT NULL)
    OR (status = 'EXECUTED'               AND checker_id IS NULL     AND decided_at IS NOT NULL)),
  CONSTRAINT metric_request_executed_shape CHECK ((status = 'EXECUTED') = (request_type = 'RETIRE_METRIC')),
  CONSTRAINT metric_request_target_shape CHECK (request_type <> 'PUBLISH_METRIC' OR revision_id IS NOT NULL),
  CONSTRAINT metric_request_reject_reason CHECK (status <> 'REJECTED' OR coalesce(decision_reason, '') <> '')
);
CREATE UNIQUE INDEX metric_request_one_pending ON kb.metric_request (metric_id) WHERE status = 'PENDING';

CREATE TRIGGER metric_revision_lock BEFORE UPDATE OR DELETE ON kb.metric_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_revision_lock();
CREATE TRIGGER metric_request_guard BEFORE UPDATE OR DELETE ON kb.metric_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_approval_guard();
CREATE TRIGGER metric_owner_only BEFORE INSERT OR UPDATE OR DELETE ON kb.metric
  FOR EACH ROW EXECUTE FUNCTION kb.tg_product_owner_only();
CREATE TRIGGER metric_revision_owner_only BEFORE INSERT OR UPDATE OR DELETE ON kb.metric_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_product_owner_only();
CREATE TRIGGER metric_request_owner_only BEFORE INSERT ON kb.metric_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_product_owner_only();
CREATE TRIGGER audit_metric          AFTER INSERT OR UPDATE OR DELETE ON kb.metric
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('metric_id');
CREATE TRIGGER audit_metric_revision AFTER INSERT OR UPDATE OR DELETE ON kb.metric_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('revision_id');
CREATE TRIGGER audit_metric_request  AFTER INSERT OR UPDATE OR DELETE ON kb.metric_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('request_id');

-- M3: the metric's columns that the product currently masks (for anyone)
CREATE OR REPLACE FUNCTION kb.metric_masked_columns(p_def jsonb) RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
  SELECT coalesce(array_agg(c ORDER BY c), '{}')
    FROM jsonb_array_elements_text(p_def->'columns') c
   WHERE EXISTS (SELECT 1 FROM kb.data_product p JOIN kb.product_revision r ON r.revision_id = p.current_revision_id
                  WHERE p.product_id = p_def->>'table' AND (r.spec->'column_masks') ? c)
$$;

CREATE OR REPLACE FUNCTION kb._metric_actor(p_metric_id text, p_roles text[]) RETURNS kb.metric
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); m kb.metric;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot govern metrics' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO m FROM kb.metric WHERE metric_id = p_metric_id FOR UPDATE;
  IF m.metric_id IS NULL THEN RAISE EXCEPTION 'unknown metric %', p_metric_id USING ERRCODE = 'P0002'; END IF;
  IF NOT EXISTS (SELECT 1 FROM kb.governance_role WHERE actor_id = me AND bu = m.gov_bu AND role = ANY(p_roles)) THEN
    RAISE EXCEPTION '% is not % for domain %', me, array_to_string(p_roles, ' or '), m.gov_bu USING ERRCODE = '42501';
  END IF;
  RETURN m;
END $$;

CREATE OR REPLACE FUNCTION kb.draft_metric(p_metric_id text, p_definition jsonb, p_description text,
                                           p_gov_bu text DEFAULT 'data')
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); m kb.metric; v_no int; v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN RAISE EXCEPTION 'pipeline role cannot govern metrics' USING ERRCODE = '42501'; END IF;
  IF NOT EXISTS (SELECT 1 FROM kb.data_product WHERE product_id = p_definition->>'table' AND status = 'ACTIVE') THEN
    RAISE EXCEPTION 'metric table % is not an ACTIVE data product', p_definition->>'table' USING ERRCODE = '23514';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM kb.metric WHERE metric_id = p_metric_id) THEN
    IF NOT kb.has_gov_role(me, p_gov_bu, 'MAKER') THEN
      RAISE EXCEPTION '% is not MAKER for domain %', me, p_gov_bu USING ERRCODE = '42501';
    END IF;
    INSERT INTO kb.metric (metric_id, product_id, gov_bu, created_by)
    VALUES (p_metric_id, p_definition->>'table', p_gov_bu, me);
  END IF;
  m := kb._metric_actor(p_metric_id, ARRAY['MAKER']);
  IF m.product_id <> p_definition->>'table' THEN RAISE EXCEPTION 'a metric cannot move to another table'; END IF;
  IF EXISTS (SELECT 1 FROM kb.metric_revision WHERE metric_id = p_metric_id AND status = 'DRAFT') THEN
    RAISE EXCEPTION 'metric % already has a draft revision', p_metric_id USING ERRCODE = '23505';
  END IF;
  SELECT coalesce(max(revision_no), 0) + 1 INTO v_no FROM kb.metric_revision WHERE metric_id = p_metric_id;
  INSERT INTO kb.metric_revision (metric_id, revision_no, base_revision_id, definition, description, created_by)
  VALUES (p_metric_id, v_no, m.current_revision_id, p_definition, p_description, me)
  RETURNING revision_id INTO v_id;
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION kb.discard_metric_draft(p_revision_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE r kb.metric_revision;
BEGIN
  SELECT * INTO r FROM kb.metric_revision WHERE revision_id = p_revision_id;
  IF r.revision_id IS NULL OR r.status <> 'DRAFT' THEN RAISE EXCEPTION 'not a draft revision'; END IF;
  IF r.created_by <> kb.current_actor() THEN RAISE EXCEPTION 'only the author can discard a draft' USING ERRCODE = '42501'; END IF;
  PERFORM kb._metric_actor(r.metric_id, ARRAY['MAKER']);
  DELETE FROM kb.metric_revision WHERE revision_id = p_revision_id;
  DELETE FROM kb.metric WHERE metric_id = r.metric_id AND status = 'DRAFT'
     AND NOT EXISTS (SELECT 1 FROM kb.metric_revision WHERE metric_id = r.metric_id);
END $$;

CREATE OR REPLACE FUNCTION kb.submit_metric(p_revision_id uuid, p_comment text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); r kb.metric_revision; masked text[]; v_id uuid;
BEGIN
  SELECT * INTO r FROM kb.metric_revision WHERE revision_id = p_revision_id FOR UPDATE;
  IF r.revision_id IS NULL OR r.status <> 'DRAFT' THEN RAISE EXCEPTION 'revision % is not a draft', p_revision_id; END IF;
  IF r.created_by <> me THEN RAISE EXCEPTION 'only the author can submit a draft' USING ERRCODE = '42501'; END IF;
  PERFORM kb._metric_actor(r.metric_id, ARRAY['MAKER']);
  masked := kb.metric_masked_columns(r.definition);
  IF cardinality(masked) > 0 THEN
    RAISE EXCEPTION 'a metric may not use masked columns: %', array_to_string(masked, ', ') USING ERRCODE = '23514';
  END IF;
  UPDATE kb.metric_revision SET status = 'SUBMITTED', locked_at = clock_timestamp() WHERE revision_id = p_revision_id;
  INSERT INTO kb.metric_request (request_type, metric_id, revision_id, maker_id, maker_comment)
  VALUES ('PUBLISH_METRIC', r.metric_id, p_revision_id, me, p_comment)
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION kb.withdraw_metric_request(p_request_id uuid, p_reason text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE r kb.metric_request;
BEGIN
  SELECT * INTO r FROM kb.metric_request WHERE request_id = p_request_id FOR UPDATE;
  IF r.request_id IS NULL THEN RAISE EXCEPTION 'unknown request %', p_request_id USING ERRCODE = 'P0002'; END IF;
  UPDATE kb.metric_request SET status = 'WITHDRAWN', decided_at = clock_timestamp(), decision_reason = p_reason
   WHERE request_id = p_request_id;
  IF r.revision_id IS NOT NULL THEN
    UPDATE kb.metric_revision SET status = 'WITHDRAWN' WHERE revision_id = r.revision_id AND status = 'SUBMITTED';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION kb.decide_metric_request(p_request_id uuid, p_approve boolean, p_reason text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); r kb.metric_request; m kb.metric; rev kb.metric_revision; masked text[];
BEGIN
  SELECT * INTO r FROM kb.metric_request WHERE request_id = p_request_id FOR UPDATE;
  IF r.request_id IS NULL THEN RAISE EXCEPTION 'unknown request %', p_request_id USING ERRCODE = 'P0002'; END IF;
  m := kb._metric_actor(r.metric_id, ARRAY['CHECKER']);
  IF me = r.maker_id THEN RAISE EXCEPTION 'maker cannot check their own request' USING ERRCODE = '42501'; END IF;
  IF r.request_type <> 'PUBLISH_METRIC' THEN RAISE EXCEPTION 'only publish requests are decided'; END IF;
  SELECT * INTO rev FROM kb.metric_revision WHERE revision_id = r.revision_id;
  IF p_approve THEN
    IF rev.base_revision_id IS DISTINCT FROM m.current_revision_id THEN
      RAISE EXCEPTION 'metric % changed after this revision was drafted; the maker must redraft', r.metric_id
        USING ERRCODE = '40001';
    END IF;
    masked := kb.metric_masked_columns(rev.definition);
    IF cardinality(masked) > 0 THEN
      RAISE EXCEPTION 'a metric may not use masked columns: %', array_to_string(masked, ', ') USING ERRCODE = '23514';
    END IF;
  END IF;
  UPDATE kb.metric_request
     SET status = CASE WHEN p_approve THEN 'APPROVED' ELSE 'REJECTED' END,
         checker_id = me, decision_reason = p_reason, decided_at = clock_timestamp()
   WHERE request_id = p_request_id;
  IF p_approve THEN
    UPDATE kb.metric_revision SET status = 'APPROVED' WHERE revision_id = r.revision_id;
    IF m.current_revision_id IS NOT NULL THEN
      UPDATE kb.metric_revision SET status = 'SUPERSEDED' WHERE revision_id = m.current_revision_id AND status = 'APPROVED';
    END IF;
    UPDATE kb.metric SET current_revision_id = r.revision_id, status = 'PUBLISHED' WHERE metric_id = r.metric_id;
  ELSE
    UPDATE kb.metric_revision SET status = 'REJECTED' WHERE revision_id = r.revision_id;
  END IF;
END $$;

-- M2: single actor; only removes an answer path (questions fall back to governed text-to-SQL)
CREATE OR REPLACE FUNCTION kb.retire_metric(p_metric_id text, p_reason text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE m kb.metric; v_id uuid;
BEGIN
  IF coalesce(p_reason, '') = '' THEN RAISE EXCEPTION 'retiring a metric needs a reason'; END IF;
  m := kb._metric_actor(p_metric_id, ARRAY['MAKER', 'CHECKER']);
  IF m.status <> 'PUBLISHED' THEN RAISE EXCEPTION 'metric % is not published', p_metric_id; END IF;
  UPDATE kb.metric SET status = 'RETIRED' WHERE metric_id = p_metric_id;
  INSERT INTO kb.metric_request (request_type, metric_id, revision_id, maker_id, maker_comment, status, decided_at)
  VALUES ('RETIRE_METRIC', p_metric_id, m.current_revision_id, kb.current_actor(), p_reason, 'EXECUTED', clock_timestamp())
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

-- what question answering reads: published metrics on ACTIVE products only
CREATE VIEW kb.metric_current AS
  SELECT m.metric_id, m.product_id, r.revision_id, r.revision_no, r.definition, r.description
    FROM kb.metric m JOIN kb.metric_revision r ON r.revision_id = m.current_revision_id
    JOIN kb.data_product p ON p.product_id = m.product_id
   WHERE m.status = 'PUBLISHED' AND r.status = 'APPROVED' AND p.status = 'ACTIVE';

CREATE OR REPLACE FUNCTION kb.metric_version() RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
  SELECT coalesce(max(extract(epoch FROM ts) * 1000000)::bigint, 0) FROM kb.audit_log
   WHERE object_type IN ('metric', 'metric_revision')
$$;

REVOKE ALL ON FUNCTION kb._metric_actor(text, text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.draft_metric(text, jsonb, text, text), kb.discard_metric_draft(uuid), kb.submit_metric(uuid, text),
                       kb.withdraw_metric_request(uuid, text), kb.decide_metric_request(uuid, boolean, text),
                       kb.retire_metric(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.draft_metric(text, jsonb, text, text), kb.discard_metric_draft(uuid), kb.submit_metric(uuid, text),
                          kb.withdraw_metric_request(uuid, text), kb.decide_metric_request(uuid, boolean, text),
                          kb.retire_metric(text, text) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.metric_masked_columns(jsonb), kb.metric_version() TO kb_app, kb_pipeline;
GRANT SELECT ON kb.metric, kb.metric_revision, kb.metric_request, kb.metric_current TO kb_app, kb_pipeline, kb_auditor;
