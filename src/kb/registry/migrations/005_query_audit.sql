-- 005_query_audit: question auditing (CLAUDE.md Phase 3) without turning the audit log into a copy
-- of what users typed.
--   Q1 every answered question writes an audit_log row (action QUERY): user, groups, filter,
--      retrieved and cited chunk ids, hashes of question and answer
--   Q2 the plain question text lives only in kb.query_log, readable by kb_auditor only; the
--      gateway role can insert but never read it back
--   Q3 audit rows written by non-owner roles always carry the session's real actor (no forgery)

CREATE TABLE kb.query_log (
  query_id    uuid        PRIMARY KEY,
  user_id     text        NOT NULL,
  question    text        NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER query_log_append_only BEFORE UPDATE OR DELETE ON kb.query_log
  FOR EACH ROW EXECUTE FUNCTION kb.tg_audit_append_only();

-- Q3
CREATE OR REPLACE FUNCTION kb.tg_audit_actor() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> kb.owner_role() THEN
    NEW.actor_id := kb.current_actor();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER audit_log_actor BEFORE INSERT ON kb.audit_log
  FOR EACH ROW EXECUTE FUNCTION kb.tg_audit_actor();

CREATE OR REPLACE FUNCTION kb.tg_query_log_user() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_user <> kb.owner_role() THEN
    NEW.user_id := kb.current_actor();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER query_log_user BEFORE INSERT ON kb.query_log
  FOR EACH ROW EXECUTE FUNCTION kb.tg_query_log_user();

REVOKE ALL ON kb.query_log FROM PUBLIC;
GRANT INSERT ON kb.query_log TO kb_app;
GRANT SELECT ON kb.query_log TO kb_auditor;
