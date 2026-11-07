-- 001_registry: GENERATED from reference/kb_registry_schema.sql (BEGIN/COMMIT removed; the runner
-- wraps each migration in one transaction). Do not edit; tests/integration/registry/test_migrations.py
-- checks that the two stay identical.

-- =============================================================================
-- kb_registry_schema.sql : Registry + maker-checker (four-eyes) schema
-- Target: PostgreSQL 17 (local: EDB binaries; CML: bank PostgreSQL)
--
-- PROVENANCE: authored by Claude on 2026-11-07 at the user's request, because the
-- original reference file was not supplied. Reviewed by: <pending>.
-- Scope: SharePoint sources only. Uploads are added by migration 002 (CLAUDE.md s5).
--
-- Four-eyes guarantees enforced HERE (not only in the API):
--   G1  checker <> maker                       CHECK constraint + function guard
--   G2  only a CHECKER for the source's BU decides   kb.governance_role lookup
--   G3  the pipeline role can never decide     no EXECUTE grant + session_user guard
--   G4  a submitted revision is immutable      locked_at + BEFORE UPDATE/DELETE trigger
--   G5  decisions are final                    BEFORE UPDATE trigger on approval_request
--   G6  a source is ACTIVE only on an APPROVED revision   trigger invariant
--   G7  every governance change is audited     AFTER trigger -> append-only audit_log
--   G8  audit_log is append-only               trigger + no UPDATE/DELETE grants
--   G9  no purge while a legal hold is active  trigger on purge_certificate
--   G10 governed state changes only inside the governance functions (current_user = schema owner),
--       never through a session setting a caller could flip
--
-- Apply as a non-superuser owner role (the migration runner does SET ROLE kb_owner) so the
-- SECURITY DEFINER functions do not run with superuser rights.
--
-- Actor identity: the gateway sets   SELECT set_config('kb.actor_id', '<user id>', true)
-- at the start of every transaction. kb.current_actor() raises if it is missing.
-- =============================================================================


CREATE SCHEMA IF NOT EXISTS kb;

-- ---------------------------------------------------------------------------
-- Group roles (NOLOGIN). Login users are created by infra and granted these.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_app')      THEN CREATE ROLE kb_app NOLOGIN;      END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_pipeline') THEN CREATE ROLE kb_pipeline NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_auditor')  THEN CREATE ROLE kb_auditor NOLOGIN;  END IF;
END $$;

-- functions created by this role are not executable by PUBLIC unless granted explicitly
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Actor context and owner identity
-- ---------------------------------------------------------------------------
-- The schema owner; inside SECURITY DEFINER governance functions current_user equals it.
CREATE OR REPLACE FUNCTION kb.owner_role() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT pg_get_userbyid(nspowner)::text FROM pg_namespace WHERE nspname = 'kb'
$$;

-- True when the session's login role is (transitively) a member of kb_pipeline. Unlike
-- pg_has_role(), this does not treat superusers as members of every role.
CREATE OR REPLACE FUNCTION kb.is_pipeline_session() RETURNS boolean
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE m(roleid) AS (
    SELECT oid FROM pg_roles WHERE rolname = session_user
    UNION
    SELECT am.roleid FROM pg_auth_members am JOIN m ON am.member = m.roleid)
  SELECT EXISTS (SELECT 1 FROM m JOIN pg_roles r ON r.oid = m.roleid WHERE r.rolname = 'kb_pipeline')
$$;

CREATE OR REPLACE FUNCTION kb.current_actor() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE a text := nullif(current_setting('kb.actor_id', true), '');
BEGIN
  IF a IS NULL THEN
    RAISE EXCEPTION 'kb.actor_id is not set for this transaction' USING ERRCODE = '28000';
  END IF;
  RETURN a;
END $$;

-- ---------------------------------------------------------------------------
-- Governance roles (synced from IdentityProvider; who may make / check per BU)
-- ---------------------------------------------------------------------------
CREATE TABLE kb.governance_role (
  actor_id   text        NOT NULL,
  bu         text        NOT NULL,
  role       text        NOT NULL CHECK (role IN ('MAKER', 'CHECKER')),
  granted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (actor_id, bu, role)
);

-- ---------------------------------------------------------------------------
-- Sources and revisions
-- ---------------------------------------------------------------------------
CREATE TABLE kb.kb_source (
  source_id           text        PRIMARY KEY,               -- deterministic slug
  bu                  text        NOT NULL,
  display_name        text        NOT NULL,
  sp_site_id          text        NOT NULL,
  sp_drive_id         text        NOT NULL,
  sp_folder_path      text        NOT NULL,
  status              text        NOT NULL DEFAULT 'DRAFT'
                      CHECK (status IN ('DRAFT', 'ACTIVE', 'SUSPENDED', 'RETIRED')),
  current_revision_id uuid,
  created_by          text        NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kb.source_revision (
  revision_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id          text        NOT NULL REFERENCES kb.kb_source(source_id),
  revision_no        int         NOT NULL CHECK (revision_no > 0),
  classification     text        NOT NULL
                     CHECK (classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
  retention_class    text        NOT NULL,
  acl_groups         text[]      NOT NULL CHECK (cardinality(acl_groups) > 0),
  allowed_extensions text[]      NOT NULL CHECK (cardinality(allowed_extensions) > 0),
  max_file_bytes     bigint      NOT NULL CHECK (max_file_bytes > 0),
  include_paths      text[]      NOT NULL DEFAULT '{}',
  exclude_paths      text[]      NOT NULL DEFAULT '{}',
  pii_policy         jsonb       NOT NULL DEFAULT '{}'::jsonb,
  status             text        NOT NULL DEFAULT 'DRAFT'
                     CHECK (status IN ('DRAFT', 'SUBMITTED', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'SUPERSEDED')),
  created_by         text        NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  locked_at          timestamptz,
  UNIQUE (source_id, revision_no),
  CHECK ((status = 'DRAFT') = (locked_at IS NULL))
);

ALTER TABLE kb.kb_source
  ADD CONSTRAINT kb_source_current_revision_fk
  FOREIGN KEY (current_revision_id) REFERENCES kb.source_revision(revision_id);

-- ---------------------------------------------------------------------------
-- Pipeline state
-- ---------------------------------------------------------------------------
CREATE TABLE kb.ingestion_run (
  run_id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id      text        NOT NULL REFERENCES kb.kb_source(source_id),
  revision_id    uuid        NOT NULL REFERENCES kb.source_revision(revision_id),
  run_kind       text        NOT NULL CHECK (run_kind IN ('DRY_RUN', 'FIRST_LOAD', 'DELTA', 'RECONCILE', 'PURGE')),
  status         text        NOT NULL DEFAULT 'QUEUED'
                 CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
  orchestrator_run_id text,
  stats          jsonb       NOT NULL DEFAULT '{}'::jsonb,
  started_at     timestamptz,
  finished_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now()
);
-- one in-flight run per source
CREATE UNIQUE INDEX ingestion_run_one_inflight
  ON kb.ingestion_run (source_id) WHERE status IN ('QUEUED', 'RUNNING');

CREATE TABLE kb.sync_state (
  source_id       text        PRIMARY KEY REFERENCES kb.kb_source(source_id),
  delta_token     text,                         -- committed only after a successful run
  last_run_id     uuid        REFERENCES kb.ingestion_run(run_id),
  last_success_at timestamptz
);

CREATE TABLE kb.document (
  doc_id             text        PRIMARY KEY,   -- deterministic: hash(source_id, sp_item_id)
  source_id          text        NOT NULL REFERENCES kb.kb_source(source_id),
  sp_item_id         text        NOT NULL,
  current_path       text        NOT NULL,
  title              text,
  status             text        NOT NULL DEFAULT 'ACTIVE'
                     CHECK (status IN ('ACTIVE', 'DELETED', 'OUT_OF_SCOPE', 'EXCLUDED')),
  current_version_no int,
  retention_until    date,
  first_seen_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at         timestamptz,
  UNIQUE (source_id, sp_item_id)
);

CREATE TABLE kb.document_version (
  version_id   text        PRIMARY KEY,         -- deterministic: doc_id || ':v' || version_no
  doc_id       text        NOT NULL REFERENCES kb.document(doc_id),
  version_no   int         NOT NULL CHECK (version_no > 0),
  ctag         text,
  sha256       char(64)    NOT NULL,
  size_bytes   bigint      NOT NULL,
  mime_type    text,
  raw_uri      text        NOT NULL,
  status       text        NOT NULL DEFAULT 'RECEIVED'
               CHECK (status IN ('RECEIVED', 'PROCESSING', 'INDEXED', 'EXCEPTION',
                                 'SUPERSEDED', 'DELETED', 'PURGED')),
  is_current   boolean     NOT NULL DEFAULT false,
  run_id       uuid        REFERENCES kb.ingestion_run(run_id),
  chunk_count  int,
  created_at   timestamptz NOT NULL DEFAULT now(),
  indexed_at   timestamptz,
  UNIQUE (doc_id, version_no)
);
CREATE UNIQUE INDEX document_version_one_current
  ON kb.document_version (doc_id) WHERE is_current;

CREATE TABLE kb.exception_item (
  exception_id uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id       text        REFERENCES kb.document(doc_id),
  version_id   text        REFERENCES kb.document_version(version_id),
  run_id       uuid        REFERENCES kb.ingestion_run(run_id),
  source_id    text        NOT NULL REFERENCES kb.kb_source(source_id),
  reason       text        NOT NULL
               CHECK (reason IN ('UNSUPPORTED_TYPE', 'SIZE_LIMIT', 'PARSE_FAILURE', 'PII_THRESHOLD')),
  detail       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  status       text        NOT NULL DEFAULT 'OPEN'
               CHECK (status IN ('OPEN', 'RELEASED', 'EXCLUDED')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  resolved_at  timestamptz
);

CREATE TABLE kb.legal_hold (
  hold_id     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id      text        REFERENCES kb.document(doc_id),
  source_id   text        REFERENCES kb.kb_source(source_id),
  reason      text        NOT NULL,
  placed_by   text        NOT NULL,
  placed_at   timestamptz NOT NULL DEFAULT now(),
  released_by text,
  released_at timestamptz,
  CHECK ((doc_id IS NULL) <> (source_id IS NULL)),
  CHECK ((released_by IS NULL) = (released_at IS NULL))
);

CREATE TABLE kb.purge_certificate (
  cert_id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id          text        NOT NULL REFERENCES kb.document(doc_id),
  version_id      text        NOT NULL REFERENCES kb.document_version(version_id),
  raw_uri         text        NOT NULL,
  sha256          char(64)    NOT NULL,
  retention_until date        NOT NULL,
  purged_by       text        NOT NULL,
  purged_at       timestamptz NOT NULL DEFAULT now(),
  evidence        jsonb       NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (version_id)
);

-- ---------------------------------------------------------------------------
-- Approval requests
-- ---------------------------------------------------------------------------
CREATE TABLE kb.approval_request (
  request_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  request_type    text        NOT NULL
                  CHECK (request_type IN ('ONBOARD_SOURCE', 'CHANGE_SOURCE', 'REACTIVATE_SOURCE',
                                          'RELEASE_EXCEPTION', 'RELEASE_LEGAL_HOLD')),
  source_id       text        NOT NULL REFERENCES kb.kb_source(source_id),
  revision_id     uuid        REFERENCES kb.source_revision(revision_id),
  exception_id    uuid        REFERENCES kb.exception_item(exception_id),
  hold_id         uuid        REFERENCES kb.legal_hold(hold_id),
  maker_id        text        NOT NULL,
  maker_comment   text,
  submitted_at    timestamptz NOT NULL DEFAULT now(),
  status          text        NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN')),
  checker_id      text,
  decision_reason text,
  decided_at      timestamptz,
  -- G1: four eyes
  CONSTRAINT approval_request_four_eyes CHECK (checker_id IS NULL OR checker_id <> maker_id),
  -- shape: decided <=> checker + time; withdrawn has no checker
  CONSTRAINT approval_request_decision_shape CHECK (
       (status = 'PENDING'               AND checker_id IS NULL     AND decided_at IS NULL)
    OR (status = 'WITHDRAWN'             AND checker_id IS NULL     AND decided_at IS NOT NULL)
    OR (status IN ('APPROVED','REJECTED') AND checker_id IS NOT NULL AND decided_at IS NOT NULL)),
  CONSTRAINT approval_request_target_shape CHECK (
       (request_type IN ('ONBOARD_SOURCE','CHANGE_SOURCE') AND revision_id  IS NOT NULL)
    OR (request_type = 'REACTIVATE_SOURCE')
    OR (request_type = 'RELEASE_EXCEPTION'  AND exception_id IS NOT NULL)
    OR (request_type = 'RELEASE_LEGAL_HOLD' AND hold_id      IS NOT NULL)),
  CONSTRAINT approval_request_reject_reason CHECK (status <> 'REJECTED' OR coalesce(decision_reason,'') <> '')
);
-- one open source-config request per source at a time
CREATE UNIQUE INDEX approval_request_one_pending_source
  ON kb.approval_request (source_id)
  WHERE status = 'PENDING' AND request_type IN ('ONBOARD_SOURCE','CHANGE_SOURCE','REACTIVATE_SOURCE');

-- ---------------------------------------------------------------------------
-- Audit log (append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE kb.audit_log (
  audit_id     bigserial   PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor_id     text        NOT NULL,
  db_user      text        NOT NULL DEFAULT session_user,
  action       text        NOT NULL,
  object_type  text        NOT NULL,
  object_id    text,
  before_state jsonb,
  after_state  jsonb,
  details      jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_log_object ON kb.audit_log (object_type, object_id);

-- =============================================================================
-- Triggers
-- =============================================================================

-- G8: append-only audit
CREATE OR REPLACE FUNCTION kb.tg_audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP USING ERRCODE = '42501';
END $$;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON kb.audit_log
  FOR EACH ROW EXECUTE FUNCTION kb.tg_audit_append_only();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON kb.audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION kb.tg_audit_append_only();

-- G7: generic governance audit (SECURITY DEFINER so callers need no INSERT on audit_log)
CREATE OR REPLACE FUNCTION kb.tg_governance_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  pk text := TG_ARGV[0];
  obj text;
BEGIN
  obj := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ->> pk ELSE to_jsonb(NEW) ->> pk END;
  INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, before_state, after_state)
  VALUES (kb.current_actor(), TG_OP, TG_TABLE_NAME, obj,
          CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
          CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END);
  RETURN NULL;
END $$;

CREATE TRIGGER audit_governance_role    AFTER INSERT OR UPDATE OR DELETE ON kb.governance_role
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('actor_id');
CREATE TRIGGER audit_kb_source          AFTER INSERT OR UPDATE OR DELETE ON kb.kb_source
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('source_id');
CREATE TRIGGER audit_source_revision    AFTER INSERT OR UPDATE OR DELETE ON kb.source_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('revision_id');
CREATE TRIGGER audit_approval_request   AFTER INSERT OR UPDATE OR DELETE ON kb.approval_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('request_id');
CREATE TRIGGER audit_legal_hold         AFTER INSERT OR UPDATE OR DELETE ON kb.legal_hold
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('hold_id');
CREATE TRIGGER audit_exception_item     AFTER UPDATE ON kb.exception_item
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('exception_id');
CREATE TRIGGER audit_purge_certificate  AFTER INSERT ON kb.purge_certificate
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('cert_id');

-- G4: a submitted (locked) revision is immutable except for its status lifecycle
CREATE OR REPLACE FUNCTION kb.tg_revision_lock() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.locked_at IS NOT NULL THEN
      RAISE EXCEPTION 'revision % is locked and cannot be deleted', OLD.revision_id USING ERRCODE = '42501';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.locked_at IS NOT NULL THEN
    IF (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
      RAISE EXCEPTION 'revision % is locked; only its status may change', OLD.revision_id USING ERRCODE = '42501';
    END IF;
    IF NOT ( (OLD.status = 'SUBMITTED' AND NEW.status IN ('APPROVED','REJECTED','WITHDRAWN'))
          OR (OLD.status = 'APPROVED'  AND NEW.status = 'SUPERSEDED')
          OR  OLD.status = NEW.status) THEN
      RAISE EXCEPTION 'illegal revision transition % -> %', OLD.status, NEW.status USING ERRCODE = '42501';
    END IF;
  ELSIF NEW.status <> 'DRAFT' AND NEW.locked_at IS NULL THEN
    RAISE EXCEPTION 'a revision leaves DRAFT only by being locked on submit' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER source_revision_lock BEFORE UPDATE OR DELETE ON kb.source_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_revision_lock();

-- G5 + G3: decisions are final; pipeline sessions never decide
CREATE OR REPLACE FUNCTION kb.tg_approval_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'approval requests cannot be deleted' USING ERRCODE = '42501';
  END IF;
  IF OLD.status <> 'PENDING' THEN
    RAISE EXCEPTION 'request % already %; decisions are final', OLD.request_id, OLD.status USING ERRCODE = '42501';
  END IF;
  IF current_user <> kb.owner_role() THEN
    RAISE EXCEPTION 'approval requests change only through the governance functions' USING ERRCODE = '42501';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['status','checker_id','decision_reason','decided_at'])
     <> (to_jsonb(OLD) - ARRAY['status','checker_id','decision_reason','decided_at']) THEN
    RAISE EXCEPTION 'only the decision fields of a request may change' USING ERRCODE = '42501';
  END IF;
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot decide approval requests' USING ERRCODE = '42501';
  END IF;
  IF NEW.status IN ('APPROVED','REJECTED') AND NEW.checker_id IS DISTINCT FROM kb.current_actor() THEN
    RAISE EXCEPTION 'checker_id must be the acting user' USING ERRCODE = '42501';
  END IF;
  IF NEW.status = 'WITHDRAWN' AND OLD.maker_id <> kb.current_actor() THEN
    RAISE EXCEPTION 'only the maker can withdraw a request' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approval_request_guard BEFORE UPDATE OR DELETE ON kb.approval_request
  FOR EACH ROW EXECUTE FUNCTION kb.tg_approval_guard();

-- G6: ACTIVE source must point at an APPROVED revision of itself
CREATE OR REPLACE FUNCTION kb.tg_source_invariant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE rs text; rsrc text;
BEGIN
  IF TG_OP = 'INSERT' AND NEW.status <> 'DRAFT' THEN
    RAISE EXCEPTION 'a source is created as DRAFT' USING ERRCODE = '42501';
  END IF;
  IF NEW.status = 'ACTIVE' THEN
    SELECT status, source_id INTO rs, rsrc FROM kb.source_revision WHERE revision_id = NEW.current_revision_id;
    IF rs IS DISTINCT FROM 'APPROVED' OR rsrc IS DISTINCT FROM NEW.source_id THEN
      RAISE EXCEPTION 'source % cannot be ACTIVE without an APPROVED current revision', NEW.source_id
        USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER kb_source_invariant BEFORE INSERT OR UPDATE ON kb.kb_source
  FOR EACH ROW EXECUTE FUNCTION kb.tg_source_invariant();

-- runs only against an ACTIVE source's approved revision (dry run: submitted or approved)
CREATE OR REPLACE FUNCTION kb.tg_run_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE s text; cur uuid; rs text;
BEGIN
  SELECT status, current_revision_id INTO s, cur FROM kb.kb_source WHERE source_id = NEW.source_id;
  SELECT status INTO rs FROM kb.source_revision WHERE revision_id = NEW.revision_id;
  IF NEW.run_kind = 'DRY_RUN' THEN
    IF rs NOT IN ('SUBMITTED','APPROVED') THEN
      RAISE EXCEPTION 'dry run needs a submitted or approved revision' USING ERRCODE = '42501';
    END IF;
  ELSIF s <> 'ACTIVE' OR NEW.revision_id IS DISTINCT FROM cur THEN
    RAISE EXCEPTION 'run % needs an ACTIVE source on its current approved revision', NEW.run_kind
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ingestion_run_guard BEFORE INSERT ON kb.ingestion_run
  FOR EACH ROW EXECUTE FUNCTION kb.tg_run_guard();

-- G9: no purge certificate while a legal hold (doc or source level) is active
CREATE OR REPLACE FUNCTION kb.tg_purge_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM kb.legal_hold h
             JOIN kb.document d ON d.doc_id = NEW.doc_id
             WHERE h.released_at IS NULL AND (h.doc_id = d.doc_id OR h.source_id = d.source_id)) THEN
    RAISE EXCEPTION 'document % is under legal hold; purge refused', NEW.doc_id USING ERRCODE = '42501';
  END IF;
  IF NEW.retention_until > current_date THEN
    RAISE EXCEPTION 'retention for % runs until %; purge refused', NEW.doc_id, NEW.retention_until
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER purge_certificate_guard BEFORE INSERT ON kb.purge_certificate
  FOR EACH ROW EXECUTE FUNCTION kb.tg_purge_guard();
CREATE TRIGGER purge_certificate_append_only BEFORE UPDATE OR DELETE ON kb.purge_certificate
  FOR EACH ROW EXECUTE FUNCTION kb.tg_audit_append_only();

-- an exception leaves OPEN only through an approved RELEASE_EXCEPTION (or EXCLUDED by a checker)
CREATE OR REPLACE FUNCTION kb.tg_exception_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> NEW.status AND current_user <> kb.owner_role() THEN
    RAISE EXCEPTION 'exception status changes only through an approval decision' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER exception_item_guard BEFORE UPDATE ON kb.exception_item
  FOR EACH ROW EXECUTE FUNCTION kb.tg_exception_guard();

-- legal holds are released only through an approved RELEASE_LEGAL_HOLD
CREATE OR REPLACE FUNCTION kb.tg_hold_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'legal holds cannot be deleted' USING ERRCODE = '42501';
  END IF;
  IF OLD.released_at IS NULL AND NEW.released_at IS NOT NULL AND current_user <> kb.owner_role() THEN
    RAISE EXCEPTION 'legal hold release needs an approved RELEASE_LEGAL_HOLD request' USING ERRCODE = '42501';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['released_by','released_at']) <> (to_jsonb(OLD) - ARRAY['released_by','released_at'])
     OR OLD.released_at IS NOT NULL THEN
    RAISE EXCEPTION 'legal hold % is immutable', OLD.hold_id USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER legal_hold_guard BEFORE UPDATE OR DELETE ON kb.legal_hold
  FOR EACH ROW EXECUTE FUNCTION kb.tg_hold_guard();

-- =============================================================================
-- Governance functions (the only way to change governed state)
-- =============================================================================

CREATE OR REPLACE FUNCTION kb.has_gov_role(p_actor text, p_bu text, p_role text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM kb.governance_role WHERE actor_id = p_actor AND bu = p_bu AND role = p_role)
$$;

-- maker: submit a request (locks the revision for source-config requests)
CREATE OR REPLACE FUNCTION kb.submit_request(
  p_type text, p_source_id text, p_revision_id uuid DEFAULT NULL,
  p_exception_id uuid DEFAULT NULL, p_hold_id uuid DEFAULT NULL, p_comment text DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor();
  v_bu text;
  v_rev kb.source_revision;
  v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot submit governance requests' USING ERRCODE = '42501';
  END IF;
  SELECT bu INTO v_bu FROM kb.kb_source WHERE source_id = p_source_id;
  IF v_bu IS NULL THEN RAISE EXCEPTION 'unknown source %', p_source_id; END IF;
  IF NOT kb.has_gov_role(me, v_bu, 'MAKER') THEN
    RAISE EXCEPTION '% is not a MAKER for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;

  IF p_type IN ('ONBOARD_SOURCE','CHANGE_SOURCE') THEN
    SELECT * INTO v_rev FROM kb.source_revision WHERE revision_id = p_revision_id FOR UPDATE;
    IF v_rev.source_id IS DISTINCT FROM p_source_id OR v_rev.status <> 'DRAFT' THEN
      RAISE EXCEPTION 'revision % is not a DRAFT of source %', p_revision_id, p_source_id;
    END IF;
    UPDATE kb.source_revision SET status = 'SUBMITTED', locked_at = clock_timestamp()
     WHERE revision_id = p_revision_id;
  END IF;

  INSERT INTO kb.approval_request (request_type, source_id, revision_id, exception_id, hold_id, maker_id, maker_comment)
  VALUES (p_type, p_source_id, p_revision_id, p_exception_id, p_hold_id, me, p_comment)
  RETURNING request_id INTO v_id;
  RETURN v_id;
END $$;

-- maker: withdraw own pending request
CREATE OR REPLACE FUNCTION kb.withdraw_request(p_request_id uuid, p_reason text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE r kb.approval_request;
BEGIN
  SELECT * INTO r FROM kb.approval_request WHERE request_id = p_request_id FOR UPDATE;
  IF r.request_id IS NULL THEN RAISE EXCEPTION 'unknown request %', p_request_id; END IF;
  UPDATE kb.approval_request
     SET status = 'WITHDRAWN', decided_at = clock_timestamp(), decision_reason = p_reason
   WHERE request_id = p_request_id;   -- tg_approval_guard checks maker = actor
  IF r.revision_id IS NOT NULL THEN
    UPDATE kb.source_revision SET status = 'WITHDRAWN' WHERE revision_id = r.revision_id;
  END IF;
END $$;

-- checker: approve or reject
CREATE OR REPLACE FUNCTION kb.decide_request(p_request_id uuid, p_approve boolean, p_reason text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor();
  r kb.approval_request;
  v_bu text;
  v_prev uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot decide approval requests' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO r FROM kb.approval_request WHERE request_id = p_request_id FOR UPDATE;
  IF r.request_id IS NULL THEN RAISE EXCEPTION 'unknown request %', p_request_id; END IF;
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
  END IF;

END $$;

-- single actor actions that only RESTRICT access (no four-eyes needed)
CREATE OR REPLACE FUNCTION kb.suspend_source(p_source_id text, p_reason text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); v_bu text;
BEGIN
  SELECT bu INTO v_bu FROM kb.kb_source WHERE source_id = p_source_id;
  IF NOT (kb.has_gov_role(me, v_bu, 'MAKER') OR kb.has_gov_role(me, v_bu, 'CHECKER')) THEN
    RAISE EXCEPTION '% has no governance role for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;
  UPDATE kb.kb_source SET status = 'SUSPENDED' WHERE source_id = p_source_id AND status = 'ACTIVE';
  INSERT INTO kb.audit_log (actor_id, action, object_type, object_id, details)
  VALUES (me, 'SUSPEND', 'kb_source', p_source_id, jsonb_build_object('reason', p_reason));
END $$;

CREATE OR REPLACE FUNCTION kb.place_legal_hold(p_doc_id text, p_source_id text, p_reason text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); v_id uuid;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot place legal holds' USING ERRCODE = '42501';
  END IF;
  INSERT INTO kb.legal_hold (doc_id, source_id, reason, placed_by)
  VALUES (p_doc_id, p_source_id, p_reason, me) RETURNING hold_id INTO v_id;
  RETURN v_id;
END $$;

-- checker excludes an exception permanently (restrictive, but recorded)
CREATE OR REPLACE FUNCTION kb.exclude_exception(p_exception_id uuid, p_reason text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); v_bu text;
BEGIN
  SELECT s.bu INTO v_bu FROM kb.exception_item e JOIN kb.kb_source s USING (source_id)
   WHERE e.exception_id = p_exception_id;
  IF NOT kb.has_gov_role(me, v_bu, 'CHECKER') THEN
    RAISE EXCEPTION '% is not a CHECKER for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;
  UPDATE kb.exception_item SET status = 'EXCLUDED', resolved_at = clock_timestamp(),
         detail = detail || jsonb_build_object('excluded_reason', p_reason)
   WHERE exception_id = p_exception_id AND status = 'OPEN';
END $$;

-- =============================================================================
-- Privileges
-- =============================================================================
REVOKE ALL ON SCHEMA kb FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA kb FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA kb FROM PUBLIC;

GRANT USAGE ON SCHEMA kb TO kb_app, kb_pipeline, kb_auditor;
GRANT SELECT ON ALL TABLES IN SCHEMA kb TO kb_app, kb_pipeline, kb_auditor;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA kb TO kb_app, kb_pipeline;

-- app (gateway): drafts only; every decision goes through functions
GRANT INSERT ON kb.kb_source TO kb_app;
GRANT UPDATE (display_name, sp_folder_path) ON kb.kb_source TO kb_app;
GRANT INSERT, DELETE ON kb.source_revision TO kb_app;
GRANT UPDATE (classification, retention_class, acl_groups, allowed_extensions, max_file_bytes,
              include_paths, exclude_paths, pii_policy) ON kb.source_revision TO kb_app;
GRANT INSERT ON kb.audit_log TO kb_app;            -- query audit (Phase 3)
GRANT EXECUTE ON FUNCTION
  kb.current_actor(), kb.owner_role(), kb.is_pipeline_session(), kb.has_gov_role(text,text,text),
  kb.submit_request(text,text,uuid,uuid,uuid,text), kb.withdraw_request(uuid,text),
  kb.decide_request(uuid,boolean,text), kb.suspend_source(text,text),
  kb.place_legal_hold(text,text,text), kb.exclude_exception(uuid,text)
  TO kb_app;

-- pipeline: document state, runs, exceptions (insert), purge certificates; never decisions
GRANT INSERT, UPDATE ON kb.document, kb.document_version, kb.sync_state TO kb_pipeline;
GRANT INSERT ON kb.ingestion_run, kb.exception_item, kb.purge_certificate, kb.audit_log TO kb_pipeline;
GRANT UPDATE (status, orchestrator_run_id, stats, started_at, finished_at) ON kb.ingestion_run TO kb_pipeline;
GRANT EXECUTE ON FUNCTION kb.current_actor(), kb.owner_role(), kb.is_pipeline_session() TO kb_pipeline;
-- deliberately NOT granted to kb_pipeline: submit_request, withdraw_request, decide_request,
-- place_legal_hold, exclude_exception, UPDATE on approval_request / legal_hold / exception_item

