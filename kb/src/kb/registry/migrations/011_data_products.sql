-- 011_data_products: mart tables registered as governed data products (Phase 9b, D-066). Each product's ACCESS
-- POLICY (table grants, row filters, column masks) lives in its approved revision; the policy engine reads the
-- current approved revision of every ACTIVE product (view kb.data_policy_current) and nothing else.
--
-- Guarantees (enforced here, not only in the API):
--   P1  maker <> checker on every product request (constraint + guard trigger, same as approval_request)
--   P2  a submitted revision is locked; only its status may change, along legal transitions
--   P3  the pipeline role can neither submit nor decide
--   P4  anything that can WIDEN access (new product, any edited revision, reactivation) needs a checker
--   P5  restricting access is single-actor (maker or checker of the domain), and the database itself computes
--       the restricted policy from the current one: revoke a group, mask a column, suspend. It cannot widen.
--   P6  approving a revision drafted against an older policy is refused, so a pending request can never
--       undo a restriction made after it was drafted
--   P7  every change is audited; every policy change bumps kb.policy_change (append-only), whose max is the
--       policy version recorded with each data query

-- ----------------------------------------------------------------------------------------------- shape check
CREATE OR REPLACE FUNCTION kb.valid_product_spec(s jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_typeof(s) = 'object'
     AND (SELECT count(*) FROM jsonb_object_keys(s) k WHERE k NOT IN ('grants', 'row_filters', 'column_masks')) = 0
     AND jsonb_typeof(s->'grants') = 'array'
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(s->'grants') g
                      WHERE jsonb_typeof(g) <> 'string' OR (g #>> '{}') !~ '^GRP_[A-Z0-9_]{2,60}$')
     AND jsonb_typeof(s->'row_filters') = 'array'
     AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(s->'row_filters') r
                      WHERE jsonb_typeof(r) <> 'object'
                         OR jsonb_typeof(r->'groups') <> 'array' OR jsonb_array_length(r->'groups') = 0
                         OR jsonb_typeof(r->'filter') <> 'string' OR length(r->>'filter') NOT BETWEEN 1 AND 500
                         OR EXISTS (SELECT 1 FROM jsonb_array_elements(r->'groups') g
                                     WHERE (g #>> '{}') !~ '^GRP_[A-Z0-9_]{2,60}$'))
     AND jsonb_typeof(s->'column_masks') = 'object'
     AND NOT EXISTS (SELECT 1 FROM jsonb_each(s->'column_masks') m
                      WHERE m.key !~ '^[a-z_][a-z0-9_]{0,62}$'
                         OR jsonb_typeof(m.value) <> 'object'
                         OR (m.value->>'mask') NOT IN ('redact', 'hash', 'year_only', 'null')
                         OR jsonb_typeof(coalesce(m.value->'unless_groups', '[]')) <> 'array')
$$;

-- ----------------------------------------------------------------------------------------------- tables
CREATE TABLE kb.data_product (
  product_id          text        PRIMARY KEY CHECK (product_id ~ '^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$'),
  gov_bu              text        NOT NULL DEFAULT 'data',   -- governance domain (maker/checker roles)
  status              text        NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'ACTIVE', 'SUSPENDED', 'RETIRED')),
  current_revision_id uuid,
  created_by          text        NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kb.product_revision (
  revision_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id       text        NOT NULL REFERENCES kb.data_product(product_id),
  revision_no      int         NOT NULL CHECK (revision_no > 0),
  base_revision_id uuid        REFERENCES kb.product_revision(revision_id),  -- the current one when drafted (P6)
  owner            text        NOT NULL CHECK (length(owner) BETWEEN 1 AND 200),
  description      text        NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
  classification   text        NOT NULL CHECK (classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
  refresh          text        NOT NULL CHECK (length(refresh) BETWEEN 1 AND 200),
  spec             jsonb       NOT NULL CHECK (kb.valid_product_spec(spec)),
  change_kind      text        NOT NULL DEFAULT 'EDIT' CHECK (change_kind IN ('EDIT', 'REVOKE_GRANT', 'ADD_MASK', 'SUSPEND')),
  change_note      text,
  status           text        NOT NULL DEFAULT 'DRAFT'
                   CHECK (status IN ('DRAFT', 'SUBMITTED', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'SUPERSEDED')),
  created_by       text        NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  locked_at        timestamptz,
  UNIQUE (product_id, revision_no),
  CHECK ((status = 'DRAFT') = (locked_at IS NULL))
);
ALTER TABLE kb.data_product ADD CONSTRAINT data_product_current_revision_fk
  FOREIGN KEY (current_revision_id) REFERENCES kb.product_revision(revision_id);

-- same shape and guarantees as kb.approval_request (G1, decision shape, reject reason), for products
CREATE TABLE kb.product_request (
  request_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  request_type    text        NOT NULL
                  CHECK (request_type IN ('ONBOARD_PRODUCT', 'CHANGE_PRODUCT', 'REACTIVATE_PRODUCT', 'RESTRICT_PRODUCT')),
  product_id      text        NOT NULL REFERENCES kb.data_product(product_id),
  revision_id     uuid        REFERENCES kb.product_revision(revision_id),
  maker_id        text        NOT NULL,
  maker_comment   text,
  submitted_at    timestamptz NOT NULL DEFAULT now(),
  status          text        NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'EXECUTED')),
  checker_id      text,
  decision_reason text,
  decided_at      timestamptz,
  CONSTRAINT product_request_four_eyes CHECK (checker_id IS NULL OR checker_id <> maker_id),
  CONSTRAINT product_request_decision_shape CHECK (
       (status = 'PENDING'                AND checker_id IS NULL     AND decided_at IS NULL)
    OR (status = 'WITHDRAWN'              AND checker_id IS NULL     AND decided_at IS NOT NULL)
    OR (status IN ('APPROVED','REJECTED') AND checker_id IS NOT NULL AND decided_at IS NOT NULL)
    OR (status = 'EXECUTED'               AND checker_id IS NULL     AND decided_at IS NOT NULL)),
  -- P5: EXECUTED (no checker) exists only for restrictions, and restrictions are only ever EXECUTED
  CONSTRAINT product_request_executed_shape CHECK ((status = 'EXECUTED') = (request_type = 'RESTRICT_PRODUCT')),
  CONSTRAINT product_request_target_shape CHECK (
       (request_type IN ('ONBOARD_PRODUCT', 'CHANGE_PRODUCT', 'RESTRICT_PRODUCT') AND revision_id IS NOT NULL)
    OR (request_type = 'REACTIVATE_PRODUCT')),
  CONSTRAINT product_request_reject_reason CHECK (status <> 'REJECTED' OR coalesce(decision_reason, '') <> '')
);
CREATE UNIQUE INDEX product_request_one_pending
  ON kb.product_request (product_id) WHERE status = 'PENDING';

-- P7: every policy-effective change; the max seq is the policy version
CREATE TABLE kb.policy_change (
  seq         bigserial   PRIMARY KEY,
  product_id  text        NOT NULL REFERENCES kb.data_product(product_id),
  revision_id uuid        REFERENCES kb.product_revision(revision_id),
  change_kind text        NOT NULL CHECK (change_kind IN ('APPROVED', 'REVOKE_GRANT', 'ADD_MASK', 'SUSPEND', 'REACTIVATE')),
  actor_id    text        NOT NULL,
  at          timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER policy_change_append_only BEFORE UPDATE OR DELETE ON kb.policy_change
  FOR EACH ROW EXECUTE FUNCTION kb.tg_append_only();

-- ----------------------------------------------------------------------------------------------- triggers
CREATE TRIGGER product_revision_lock BEFORE UPDATE OR DELETE ON kb.product_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_revision_lock();          -- P2 (same function as source revisions)
CREATE TRIGGER product_request_guard BEFORE UPDATE OR DELETE ON kb.product_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_approval_guard();         -- final decisions, pipeline never decides

-- rows are created only through the governance functions below (owner role); app/pipeline roles get no DML
CREATE OR REPLACE FUNCTION kb.tg_product_owner_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> kb.owner_role() THEN
    RAISE EXCEPTION '% changes only through the data-product functions', TG_TABLE_NAME USING ERRCODE = '42501';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER data_product_owner_only BEFORE INSERT OR UPDATE OR DELETE ON kb.data_product
  FOR EACH ROW EXECUTE FUNCTION kb.tg_product_owner_only();
CREATE TRIGGER product_revision_owner_only BEFORE INSERT OR UPDATE OR DELETE ON kb.product_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_product_owner_only();
CREATE TRIGGER product_request_owner_only BEFORE INSERT ON kb.product_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_product_owner_only();

-- an ACTIVE product points at an APPROVED revision of itself
CREATE OR REPLACE FUNCTION kb.tg_product_invariant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE rs text; rp text;
BEGIN
  IF NEW.status = 'ACTIVE' THEN
    SELECT status, product_id INTO rs, rp FROM kb.product_revision WHERE revision_id = NEW.current_revision_id;
    IF rs IS DISTINCT FROM 'APPROVED' OR rp IS DISTINCT FROM NEW.product_id THEN
      RAISE EXCEPTION 'ACTIVE product % must point at an APPROVED revision of itself', NEW.product_id
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER data_product_invariant BEFORE INSERT OR UPDATE ON kb.data_product
  FOR EACH ROW EXECUTE FUNCTION kb.tg_product_invariant();

CREATE TRIGGER audit_data_product     AFTER INSERT OR UPDATE OR DELETE ON kb.data_product
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('product_id');
CREATE TRIGGER audit_product_revision AFTER INSERT OR UPDATE OR DELETE ON kb.product_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('revision_id');
CREATE TRIGGER audit_product_request  AFTER INSERT OR UPDATE OR DELETE ON kb.product_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('request_id');

-- ----------------------------------------------------------------------------------------------- functions
CREATE OR REPLACE FUNCTION kb._product_actor(p_product_id text, p_roles text[]) RETURNS kb.data_product
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); p kb.data_product;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot govern data products' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO p FROM kb.data_product WHERE product_id = p_product_id FOR UPDATE;
  IF p.product_id IS NULL THEN RAISE EXCEPTION 'unknown data product %', p_product_id USING ERRCODE = 'P0002'; END IF;
  IF NOT EXISTS (SELECT 1 FROM kb.governance_role WHERE actor_id = me AND bu = p.gov_bu AND role = ANY(p_roles)) THEN
    RAISE EXCEPTION '% is not % for domain %', me, array_to_string(p_roles, ' or '), p.gov_bu USING ERRCODE = '42501';
  END IF;
  RETURN p;
END $$;

-- maker: create a DRAFT revision (and the product itself, first time). Returns the revision id.
CREATE OR REPLACE FUNCTION kb.draft_product(
  p_product_id text, p_owner text, p_description text, p_classification text, p_refresh text, p_spec jsonb,
  p_gov_bu text DEFAULT 'data')
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); p kb.data_product; v_no int; v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot govern data products' USING ERRCODE = '42501';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM kb.data_product WHERE product_id = p_product_id) THEN
    IF NOT kb.has_gov_role(me, p_gov_bu, 'MAKER') THEN
      RAISE EXCEPTION '% is not MAKER for domain %', me, p_gov_bu USING ERRCODE = '42501';
    END IF;
    INSERT INTO kb.data_product (product_id, gov_bu, created_by) VALUES (p_product_id, p_gov_bu, me);
  END IF;
  p := kb._product_actor(p_product_id, ARRAY['MAKER']);
  IF EXISTS (SELECT 1 FROM kb.product_revision WHERE product_id = p_product_id AND status = 'DRAFT') THEN
    RAISE EXCEPTION 'product % already has a draft revision', p_product_id USING ERRCODE = '23505';
  END IF;
  SELECT coalesce(max(revision_no), 0) + 1 INTO v_no FROM kb.product_revision WHERE product_id = p_product_id;
  INSERT INTO kb.product_revision (product_id, revision_no, base_revision_id, owner, description, classification,
                                   refresh, spec, created_by)
  VALUES (p_product_id, v_no, p.current_revision_id, p_owner, p_description, p_classification, p_refresh, p_spec, me)
  RETURNING revision_id INTO v_id;
  RETURN v_id;
END $$;

-- maker: discard own draft
CREATE OR REPLACE FUNCTION kb.discard_product_draft(p_revision_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE r kb.product_revision;
BEGIN
  SELECT * INTO r FROM kb.product_revision WHERE revision_id = p_revision_id;
  IF r.revision_id IS NULL OR r.status <> 'DRAFT' THEN RAISE EXCEPTION 'not a draft revision'; END IF;
  IF r.created_by <> kb.current_actor() THEN
    RAISE EXCEPTION 'only the author can discard a draft' USING ERRCODE = '42501';
  END IF;
  PERFORM kb._product_actor(r.product_id, ARRAY['MAKER']);
  DELETE FROM kb.product_revision WHERE revision_id = p_revision_id;
  DELETE FROM kb.data_product WHERE product_id = r.product_id AND status = 'DRAFT'
     AND NOT EXISTS (SELECT 1 FROM kb.product_revision WHERE product_id = r.product_id);
END $$;

-- maker: submit a draft (locks it). A product with no grants is refused: deny-all is SUSPEND, not a revision.
CREATE OR REPLACE FUNCTION kb.submit_product(p_revision_id uuid, p_comment text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); r kb.product_revision; p kb.data_product; v_id uuid;
BEGIN
  SELECT * INTO r FROM kb.product_revision WHERE revision_id = p_revision_id FOR UPDATE;
  IF r.revision_id IS NULL OR r.status <> 'DRAFT' THEN RAISE EXCEPTION 'revision % is not a draft', p_revision_id; END IF;
  IF r.created_by <> me THEN RAISE EXCEPTION 'only the author can submit a draft' USING ERRCODE = '42501'; END IF;
  p := kb._product_actor(r.product_id, ARRAY['MAKER']);
  IF jsonb_array_length(r.spec->'grants') = 0 THEN
    RAISE EXCEPTION 'a product revision must grant at least one group (use suspend to deny all)' USING ERRCODE = '23514';
  END IF;
  UPDATE kb.product_revision SET status = 'SUBMITTED', locked_at = clock_timestamp() WHERE revision_id = p_revision_id;
  INSERT INTO kb.product_request (request_type, product_id, revision_id, maker_id, maker_comment)
  VALUES (CASE WHEN p.current_revision_id IS NULL THEN 'ONBOARD_PRODUCT' ELSE 'CHANGE_PRODUCT' END,
          r.product_id, p_revision_id, me, p_comment)
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

-- maker: request reactivation of a SUSPENDED product (widens access -> checker)
CREATE OR REPLACE FUNCTION kb.request_product_reactivation(p_product_id text, p_comment text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE p kb.data_product; v_id uuid;
BEGIN
  p := kb._product_actor(p_product_id, ARRAY['MAKER']);
  IF p.status <> 'SUSPENDED' THEN RAISE EXCEPTION 'product % is not suspended', p_product_id; END IF;
  INSERT INTO kb.product_request (request_type, product_id, maker_id, maker_comment)
  VALUES ('REACTIVATE_PRODUCT', p_product_id, kb.current_actor(), p_comment)
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

-- maker: withdraw own pending request
CREATE OR REPLACE FUNCTION kb.withdraw_product_request(p_request_id uuid, p_reason text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE r kb.product_request;
BEGIN
  SELECT * INTO r FROM kb.product_request WHERE request_id = p_request_id FOR UPDATE;
  IF r.request_id IS NULL THEN RAISE EXCEPTION 'unknown request %', p_request_id USING ERRCODE = 'P0002'; END IF;
  UPDATE kb.product_request SET status = 'WITHDRAWN', decided_at = clock_timestamp(), decision_reason = p_reason
   WHERE request_id = p_request_id;                       -- guard: only the maker may withdraw
  IF r.revision_id IS NOT NULL THEN
    UPDATE kb.product_revision SET status = 'WITHDRAWN' WHERE revision_id = r.revision_id AND status = 'SUBMITTED';
  END IF;
END $$;

-- checker: approve or reject (P1, P3, P4, P6)
CREATE OR REPLACE FUNCTION kb.decide_product_request(p_request_id uuid, p_approve boolean, p_reason text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); r kb.product_request; p kb.data_product; rev kb.product_revision;
BEGIN
  SELECT * INTO r FROM kb.product_request WHERE request_id = p_request_id FOR UPDATE;
  IF r.request_id IS NULL THEN RAISE EXCEPTION 'unknown request %', p_request_id USING ERRCODE = 'P0002'; END IF;
  p := kb._product_actor(r.product_id, ARRAY['CHECKER']);
  IF me = r.maker_id THEN RAISE EXCEPTION 'maker cannot check their own request' USING ERRCODE = '42501'; END IF;
  IF r.request_type = 'RESTRICT_PRODUCT' THEN RAISE EXCEPTION 'restrictions are not decided'; END IF;
  IF p_approve AND r.revision_id IS NOT NULL THEN
    SELECT * INTO rev FROM kb.product_revision WHERE revision_id = r.revision_id;
    IF rev.base_revision_id IS DISTINCT FROM p.current_revision_id THEN
      RAISE EXCEPTION 'product % changed after this revision was drafted; the maker must redraft', r.product_id
        USING ERRCODE = '40001';
    END IF;
  END IF;

  UPDATE kb.product_request
     SET status = CASE WHEN p_approve THEN 'APPROVED' ELSE 'REJECTED' END,
         checker_id = me, decision_reason = p_reason, decided_at = clock_timestamp()
   WHERE request_id = p_request_id;

  IF r.request_type IN ('ONBOARD_PRODUCT', 'CHANGE_PRODUCT') THEN
    IF p_approve THEN
      UPDATE kb.product_revision SET status = 'APPROVED' WHERE revision_id = r.revision_id;
      IF p.current_revision_id IS NOT NULL THEN
        UPDATE kb.product_revision SET status = 'SUPERSEDED' WHERE revision_id = p.current_revision_id AND status = 'APPROVED';
      END IF;
      UPDATE kb.data_product SET current_revision_id = r.revision_id,
             status = CASE WHEN status = 'SUSPENDED' THEN 'SUSPENDED' ELSE 'ACTIVE' END
       WHERE product_id = r.product_id;
      INSERT INTO kb.policy_change (product_id, revision_id, change_kind, actor_id)
      VALUES (r.product_id, r.revision_id, 'APPROVED', me);
    ELSE
      UPDATE kb.product_revision SET status = 'REJECTED' WHERE revision_id = r.revision_id;
    END IF;
  ELSIF r.request_type = 'REACTIVATE_PRODUCT' AND p_approve THEN
    UPDATE kb.data_product SET status = 'ACTIVE' WHERE product_id = r.product_id AND status = 'SUSPENDED';
    INSERT INTO kb.policy_change (product_id, revision_id, change_kind, actor_id)
    VALUES (r.product_id, p.current_revision_id, 'REACTIVATE', me);
  END IF;
END $$;

-- P5: single-actor restriction. The database derives the new policy from the current one; it can only narrow.
--   REVOKE_GRANT  p_group leaves grants, every row-filter entry and every mask exemption
--   ADD_MASK      p_column gets p_mask if unmasked; if already masked, only its exemptions are cleared
--   SUSPEND       the product is denied to everyone (status SUSPENDED); reactivation needs a checker
CREATE OR REPLACE FUNCTION kb.restrict_product(
  p_product_id text, p_action text, p_reason text, p_group text DEFAULT NULL, p_column text DEFAULT NULL,
  p_mask text DEFAULT 'redact')
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor(); p kb.data_product; cur kb.product_revision; s jsonb; v_rev uuid; v_req uuid;
  v_no int; f jsonb; filters jsonb := '[]'::jsonb; masks jsonb := '{}'::jsonb; m record;
BEGIN
  IF coalesce(p_reason, '') = '' THEN RAISE EXCEPTION 'a restriction needs a reason'; END IF;
  p := kb._product_actor(p_product_id, ARRAY['MAKER', 'CHECKER']);
  IF p.status <> 'ACTIVE' THEN RAISE EXCEPTION 'product % is not ACTIVE', p_product_id; END IF;
  SELECT * INTO cur FROM kb.product_revision WHERE revision_id = p.current_revision_id;
  s := cur.spec;

  IF p_action = 'SUSPEND' THEN
    UPDATE kb.data_product SET status = 'SUSPENDED' WHERE product_id = p_product_id;
    INSERT INTO kb.product_request (request_type, product_id, revision_id, maker_id, maker_comment, status, decided_at)
    VALUES ('RESTRICT_PRODUCT', p_product_id, cur.revision_id, me, 'SUSPEND: ' || p_reason, 'EXECUTED', clock_timestamp())
    RETURNING request_id INTO v_req;
    INSERT INTO kb.policy_change (product_id, revision_id, change_kind, actor_id)
    VALUES (p_product_id, cur.revision_id, 'SUSPEND', me);
    RETURN v_req;
  ELSIF p_action = 'REVOKE_GRANT' THEN
    IF p_group IS NULL OR NOT (s->'grants') ? p_group THEN
      RAISE EXCEPTION 'group % is not granted on %', p_group, p_product_id;
    END IF;
    FOR f IN SELECT * FROM jsonb_array_elements(s->'row_filters') LOOP
      f := jsonb_set(f, '{groups}', (f->'groups') - p_group);
      IF jsonb_array_length(f->'groups') > 0 THEN filters := filters || jsonb_build_array(f); END IF;
    END LOOP;
    FOR m IN SELECT key, value FROM jsonb_each(s->'column_masks') LOOP
      masks := masks || jsonb_build_object(m.key,
                 jsonb_set(m.value, '{unless_groups}', coalesce(m.value->'unless_groups', '[]') - p_group));
    END LOOP;
    s := jsonb_build_object('grants', (s->'grants') - p_group, 'row_filters', filters, 'column_masks', masks);
  ELSIF p_action = 'ADD_MASK' THEN
    IF p_column IS NULL OR p_column !~ '^[a-z_][a-z0-9_]{0,62}$' THEN RAISE EXCEPTION 'a column is required'; END IF;
    IF p_mask NOT IN ('redact', 'hash', 'year_only', 'null') THEN RAISE EXCEPTION 'unknown mask %', p_mask; END IF;
    IF (s->'column_masks') ? p_column THEN
      s := jsonb_set(s, ARRAY['column_masks', p_column, 'unless_groups'], '[]'::jsonb);  -- keep its mask type
    ELSE
      s := jsonb_set(s, ARRAY['column_masks', p_column], jsonb_build_object('mask', p_mask, 'unless_groups', '[]'::jsonb));
    END IF;
  ELSE
    RAISE EXCEPTION 'unknown restriction %', p_action;
  END IF;

  SELECT max(revision_no) + 1 INTO v_no FROM kb.product_revision WHERE product_id = p_product_id;
  INSERT INTO kb.product_revision (product_id, revision_no, base_revision_id, owner, description, classification,
                                   refresh, spec, change_kind, change_note, status, created_by, locked_at)
  VALUES (p_product_id, v_no, cur.revision_id, cur.owner, cur.description, cur.classification, cur.refresh, s,
          p_action, p_reason, 'APPROVED', me, clock_timestamp())
  RETURNING revision_id INTO v_rev;
  UPDATE kb.product_revision SET status = 'SUPERSEDED' WHERE revision_id = cur.revision_id;
  UPDATE kb.data_product SET current_revision_id = v_rev WHERE product_id = p_product_id;
  INSERT INTO kb.product_request (request_type, product_id, revision_id, maker_id, maker_comment, status, decided_at)
  VALUES ('RESTRICT_PRODUCT', p_product_id, v_rev, me, p_action || ': ' || p_reason, 'EXECUTED', clock_timestamp())
  RETURNING request_id INTO v_req;
  INSERT INTO kb.policy_change (product_id, revision_id, change_kind, actor_id) VALUES (p_product_id, v_rev, p_action, me);
  RETURN v_req;
END $$;

-- ----------------------------------------------------------------------------------------------- policy view
-- The ONLY thing the policy engine reads: the approved spec of every ACTIVE product. A SUSPENDED, DRAFT or RETIRED
-- product contributes nothing, so its table is denied (deny by default).
CREATE VIEW kb.data_policy_current AS
  SELECT p.product_id, r.revision_id, r.revision_no, r.spec
    FROM kb.data_product p JOIN kb.product_revision r ON r.revision_id = p.current_revision_id
   WHERE p.status = 'ACTIVE' AND r.status = 'APPROVED';

CREATE OR REPLACE FUNCTION kb.policy_version() RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
  SELECT coalesce(max(seq), 0) FROM kb.policy_change
$$;

-- ----------------------------------------------------------------------------------------------- privileges
REVOKE ALL ON FUNCTION kb._product_actor(text, text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.draft_product(text, text, text, text, text, jsonb, text), kb.discard_product_draft(uuid),
                       kb.submit_product(uuid, text), kb.request_product_reactivation(text, text),
                       kb.withdraw_product_request(uuid, text), kb.decide_product_request(uuid, boolean, text),
                       kb.restrict_product(text, text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.draft_product(text, text, text, text, text, jsonb, text), kb.discard_product_draft(uuid),
                          kb.submit_product(uuid, text), kb.request_product_reactivation(text, text),
                          kb.withdraw_product_request(uuid, text), kb.decide_product_request(uuid, boolean, text),
                          kb.restrict_product(text, text, text, text, text, text) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.policy_version() TO kb_app, kb_pipeline;
GRANT SELECT ON kb.data_product, kb.product_revision, kb.product_request, kb.policy_change, kb.data_policy_current
  TO kb_app, kb_pipeline, kb_auditor;
