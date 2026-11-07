-- 003_draft_authorship: sources and revision drafts are written only by a MAKER of the source's BU,
-- and authorship is the acting user (never a caller-supplied value). Governance functions (running
-- as the schema owner) are exempt.

CREATE OR REPLACE FUNCTION kb.tg_draft_author() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  me  text;
  v_bu text;
  rec record := CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
BEGIN
  IF current_user = kb.owner_role() THEN
    RETURN rec;
  END IF;
  -- G10 for drafts: lifecycle status (and the source's governed pointers) change only inside the
  -- governance functions, whatever privileges the caller holds
  IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) ->> 'status' IS DISTINCT FROM to_jsonb(OLD) ->> 'status'
      OR to_jsonb(NEW) ->> 'current_revision_id' IS DISTINCT FROM to_jsonb(OLD) ->> 'current_revision_id'
      OR to_jsonb(NEW) ->> 'locked_at' IS DISTINCT FROM to_jsonb(OLD) ->> 'locked_at') THEN
    RAISE EXCEPTION 'status changes only through the governance functions' USING ERRCODE = '42501';
  END IF;
  me := kb.current_actor();
  IF TG_TABLE_NAME = 'kb_source' THEN
    v_bu := rec.bu;
  ELSE
    SELECT bu INTO v_bu FROM kb.kb_source WHERE source_id = rec.source_id;
  END IF;
  IF NOT kb.has_gov_role(me, v_bu, 'MAKER') THEN
    RAISE EXCEPTION '% is not a MAKER for BU %', me, v_bu USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'INSERT' THEN
    NEW.created_by := me;
    RETURN NEW;
  END IF;
  RETURN rec;
END $$;

CREATE TRIGGER kb_source_draft_author BEFORE INSERT OR UPDATE ON kb.kb_source
  FOR EACH ROW EXECUTE FUNCTION kb.tg_draft_author();
CREATE TRIGGER source_revision_draft_author BEFORE INSERT OR UPDATE OR DELETE ON kb.source_revision
  FOR EACH ROW EXECUTE FUNCTION kb.tg_draft_author();
