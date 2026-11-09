-- 010_data_quality: data-quality rule results on the marts (Phase 9a, D-065). Append-only; written only through
-- kb.record_dq_run(), which only the pipeline role may call. Only COUNTS are stored (rows checked, rows failing):
-- never failing values or row identifiers, so a result row can be shown to anyone granted the table.

CREATE TABLE kb.dq_run (
  run_id       uuid PRIMARY KEY,
  started_at   timestamptz NOT NULL,
  finished_at  timestamptz NOT NULL,
  as_of        date NOT NULL,               -- reference date for freshness rules
  rules_sha256 text NOT NULL,               -- hash of config/dq_rules.yaml as run
  recorded_by  text NOT NULL,
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (finished_at >= started_at)
);

CREATE TABLE kb.dq_result (
  run_id      uuid NOT NULL REFERENCES kb.dq_run (run_id),
  rule_id     text NOT NULL,
  table_name  text NOT NULL,
  rule_kind   text NOT NULL CHECK (rule_kind IN ('not_null', 'range', 'unique', 'allowed_values', 'max_age', 'reference')),
  column_name text NOT NULL,
  checked     bigint NOT NULL CHECK (checked >= 0),
  failed      bigint NOT NULL CHECK (failed >= 0 AND failed <= checked),
  tolerance   numeric NOT NULL CHECK (tolerance >= 0 AND tolerance <= 1),
  passed      boolean NOT NULL,
  error       text,                         -- the rule could not run (e.g. timeout); passed is then false
  PRIMARY KEY (run_id, rule_id)
);
CREATE INDEX dq_run_started ON kb.dq_run (started_at DESC);

CREATE TRIGGER dq_run_append_only BEFORE UPDATE OR DELETE ON kb.dq_run
  FOR EACH ROW EXECUTE FUNCTION kb.tg_append_only();
CREATE TRIGGER dq_result_append_only BEFORE UPDATE OR DELETE ON kb.dq_result
  FOR EACH ROW EXECUTE FUNCTION kb.tg_append_only();

-- p_run: {run_id, started_at, finished_at, as_of, rules_sha256}
-- p_results: [{rule_id, table_name, rule_kind, column_name, checked, failed, tolerance, passed, error?}, ...]
-- A retry with the same run_id and the same number of results is a no-op; anything else under that id is refused.
CREATE OR REPLACE FUNCTION kb.record_dq_run(p_run jsonb, p_results jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor();
  v_id uuid := (p_run->>'run_id')::uuid;
  r jsonb;
BEGIN
  IF NOT kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'only the pipeline role records data-quality runs' USING ERRCODE = '42501';
  END IF;
  IF jsonb_typeof(p_results) IS DISTINCT FROM 'array' OR jsonb_array_length(p_results) = 0 THEN
    RAISE EXCEPTION 'a data-quality run needs at least one result';
  END IF;
  IF EXISTS (SELECT 1 FROM kb.dq_run WHERE run_id = v_id) THEN
    IF (SELECT count(*) FROM kb.dq_result WHERE run_id = v_id) = jsonb_array_length(p_results)
       AND (SELECT rules_sha256 FROM kb.dq_run WHERE run_id = v_id) = p_run->>'rules_sha256' THEN
      RETURN v_id;
    END IF;
    RAISE EXCEPTION 'data-quality run % already recorded with different content', v_id USING ERRCODE = '23505';
  END IF;
  INSERT INTO kb.dq_run (run_id, started_at, finished_at, as_of, rules_sha256, recorded_by)
  VALUES (v_id, (p_run->>'started_at')::timestamptz, (p_run->>'finished_at')::timestamptz,
          (p_run->>'as_of')::date, p_run->>'rules_sha256', me);
  FOR r IN SELECT * FROM jsonb_array_elements(p_results) LOOP
    INSERT INTO kb.dq_result (run_id, rule_id, table_name, rule_kind, column_name, checked, failed, tolerance, passed, error)
    VALUES (v_id, r->>'rule_id', r->>'table_name', r->>'rule_kind', r->>'column_name', (r->>'checked')::bigint,
            (r->>'failed')::bigint, (r->>'tolerance')::numeric, (r->>'passed')::boolean, r->>'error');
  END LOOP;
  RETURN v_id;
END $$;

REVOKE ALL ON FUNCTION kb.record_dq_run(jsonb, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.record_dq_run(jsonb, jsonb) TO kb_pipeline;
GRANT SELECT ON kb.dq_run, kb.dq_result TO kb_app, kb_pipeline, kb_auditor;
