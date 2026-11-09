-- 006_concept_review: review queue for low-confidence FIBO mappings (graphify-fibo skill, step 6).
--   R1 the pipeline proposes; only a CHECKER of the mapping's BU decides (maker = pipeline, so maker != checker)
--   R2 decisions change status only inside kb.decide_concept (G10 style), are final, and are audited
--   R3 nothing PENDING is used for graph expansion (the build reads ACCEPTED only)

CREATE TABLE kb.concept_review (
  review_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  bu               text        NOT NULL,
  entity_iri       text        NOT NULL,
  entity_label     text        NOT NULL,
  proposed_concept text        NOT NULL,
  proposed_label   text        NOT NULL,
  confidence       numeric(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  method           text        NOT NULL,
  chunk_ids        text[]      NOT NULL DEFAULT '{}',
  context          text,
  run_id           text,
  status           text        NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED')),
  decided_by       text,
  decided_at       timestamptz,
  decision_reason  text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (bu, entity_iri, proposed_concept),
  CHECK ((status = 'PENDING') = (decided_by IS NULL AND decided_at IS NULL))
);

CREATE OR REPLACE FUNCTION kb.tg_concept_review_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'concept reviews cannot be deleted' USING ERRCODE = '42501';
  END IF;
  IF OLD.status <> 'PENDING' THEN
    RAISE EXCEPTION 'concept review % already %; decisions are final', OLD.review_id, OLD.status
      USING ERRCODE = '42501';
  END IF;
  IF current_user <> kb.owner_role() THEN
    RAISE EXCEPTION 'concept reviews change only through kb.decide_concept' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER concept_review_guard BEFORE UPDATE OR DELETE ON kb.concept_review
  FOR EACH ROW EXECUTE FUNCTION kb.tg_concept_review_guard();
CREATE TRIGGER audit_concept_review AFTER UPDATE ON kb.concept_review
  FOR EACH ROW EXECUTE FUNCTION kb.tg_governance_audit('review_id');

CREATE OR REPLACE FUNCTION kb.decide_concept(p_review_id uuid, p_accept boolean, p_reason text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE me text := kb.current_actor(); r kb.concept_review;
BEGIN
  IF kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'pipeline role cannot decide concept reviews' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO r FROM kb.concept_review WHERE review_id = p_review_id FOR UPDATE;
  IF r.review_id IS NULL THEN RAISE EXCEPTION 'unknown concept review %', p_review_id USING ERRCODE = 'P0002'; END IF;
  IF NOT kb.has_gov_role(me, r.bu, 'CHECKER') THEN
    RAISE EXCEPTION '% is not a CHECKER for BU %', me, r.bu USING ERRCODE = '42501';
  END IF;
  IF NOT p_accept AND coalesce(p_reason, '') = '' THEN
    RAISE EXCEPTION 'a reason is required to reject a mapping';
  END IF;
  UPDATE kb.concept_review
     SET status = CASE WHEN p_accept THEN 'ACCEPTED' ELSE 'REJECTED' END,
         decided_by = me, decided_at = clock_timestamp(), decision_reason = p_reason
   WHERE review_id = p_review_id;
END $$;

REVOKE ALL ON kb.concept_review FROM PUBLIC;
REVOKE ALL ON FUNCTION kb.decide_concept(uuid, boolean, text) FROM PUBLIC;
GRANT SELECT ON kb.concept_review TO kb_app, kb_pipeline, kb_auditor;
GRANT INSERT ON kb.concept_review TO kb_pipeline;
GRANT EXECUTE ON FUNCTION kb.decide_concept(uuid, boolean, text) TO kb_app;
