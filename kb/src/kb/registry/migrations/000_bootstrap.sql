-- 000_bootstrap: cluster roles and database privileges. Runs as the admin (superuser) role.
-- Idempotent. Passwords for login roles are set by the migration runner, never stored here.

DO $$
BEGIN
  -- owner of schema kb and of every SECURITY DEFINER function: deliberately NOT a superuser
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_owner')    THEN CREATE ROLE kb_owner NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_app')      THEN CREATE ROLE kb_app NOLOGIN;      END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_pipeline') THEN CREATE ROLE kb_pipeline NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_auditor')  THEN CREATE ROLE kb_auditor NOLOGIN;  END IF;
  -- login roles used by services
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_api')  THEN CREATE ROLE kb_api LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_pipe') THEN CREATE ROLE kb_pipe LOGIN; END IF;
END $$;

-- each login belongs to exactly one group role
GRANT kb_app TO kb_api;
GRANT kb_pipeline TO kb_pipe;

DO $$
BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO kb_app, kb_pipeline, kb_auditor, kb_owner', current_database());
  EXECUTE format('GRANT CREATE ON DATABASE %I TO kb_owner', current_database());
END $$;

-- functions kb_owner creates later are not executable by PUBLIC by default
ALTER DEFAULT PRIVILEGES FOR ROLE kb_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
