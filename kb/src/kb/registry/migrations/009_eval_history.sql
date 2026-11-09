-- 009_eval_history: every `make eval` run and its per-BU metrics, for the answer-quality trend and the
-- leak-suite status on the ops dashboard. Append-only; written only through kb.record_eval_run(), which
-- only the pipeline role may call (evaluation is an operations job, not a user action).

CREATE TABLE kb.eval_run (
  run_id       uuid PRIMARY KEY,
  started_at   timestamptz NOT NULL,
  finished_at  timestamptz NOT NULL,
  git_commit   text NOT NULL,
  status       text NOT NULL CHECK (status IN ('PASS', 'FAIL')),
  config       jsonb NOT NULL,           -- models, index, thresholds, suites run
  summary      jsonb NOT NULL,           -- headline numbers and failure reasons
  report_uri   text,
  recorded_by  text NOT NULL,
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (finished_at >= started_at)
);

CREATE TABLE kb.eval_result (
  run_id     uuid NOT NULL REFERENCES kb.eval_run (run_id),
  bu         text NOT NULL,              -- retail | corporate | wealth | shared | all
  suite      text NOT NULL CHECK (suite IN ('golden', 'ragas', 'leak', 'expansion', 'structured')),
  metric     text NOT NULL,
  value      numeric NOT NULL,
  n          integer NOT NULL CHECK (n >= 0),
  threshold  numeric,
  passed     boolean,
  PRIMARY KEY (run_id, bu, suite, metric)
);
CREATE INDEX eval_run_started ON kb.eval_run (started_at DESC);

CREATE TRIGGER eval_run_append_only BEFORE UPDATE OR DELETE ON kb.eval_run
  FOR EACH ROW EXECUTE FUNCTION kb.tg_append_only();
CREATE TRIGGER eval_result_append_only BEFORE UPDATE OR DELETE ON kb.eval_result
  FOR EACH ROW EXECUTE FUNCTION kb.tg_append_only();

-- p_run: {run_id, started_at, finished_at, git_commit, status, config, summary, report_uri}
-- p_results: [{bu, suite, metric, value, n, threshold?, passed?}, ...]
-- A retry of the same run (same run_id and same content) is a no-op; a different payload under an existing
-- run_id is refused.
CREATE OR REPLACE FUNCTION kb.record_eval_run(p_run jsonb, p_results jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = kb, pg_catalog AS $$
DECLARE
  me text := kb.current_actor();
  v_id uuid := (p_run->>'run_id')::uuid;
  existing kb.eval_run;
  r jsonb;
BEGIN
  IF NOT kb.is_pipeline_session() THEN
    RAISE EXCEPTION 'only the pipeline role records evaluation runs' USING ERRCODE = '42501';
  END IF;
  IF jsonb_typeof(p_results) IS DISTINCT FROM 'array' OR jsonb_array_length(p_results) = 0 THEN
    RAISE EXCEPTION 'an evaluation run needs at least one result';
  END IF;
  SELECT * INTO existing FROM kb.eval_run WHERE run_id = v_id;
  IF FOUND THEN
    IF existing.summary = p_run->'summary' AND existing.status = p_run->>'status'
       AND (SELECT count(*) FROM kb.eval_result WHERE run_id = v_id) = jsonb_array_length(p_results) THEN
      RETURN v_id;
    END IF;
    RAISE EXCEPTION 'evaluation run % already recorded with different content', v_id USING ERRCODE = '23505';
  END IF;
  INSERT INTO kb.eval_run (run_id, started_at, finished_at, git_commit, status, config, summary, report_uri, recorded_by)
  VALUES (v_id, (p_run->>'started_at')::timestamptz, (p_run->>'finished_at')::timestamptz, p_run->>'git_commit',
          p_run->>'status', coalesce(p_run->'config', '{}'::jsonb), coalesce(p_run->'summary', '{}'::jsonb),
          p_run->>'report_uri', me);
  FOR r IN SELECT * FROM jsonb_array_elements(p_results) LOOP
    INSERT INTO kb.eval_result (run_id, bu, suite, metric, value, n, threshold, passed)
    VALUES (v_id, r->>'bu', r->>'suite', r->>'metric', (r->>'value')::numeric, (r->>'n')::int,
            (r->>'threshold')::numeric, (r->>'passed')::boolean);
  END LOOP;
  RETURN v_id;
END $$;

REVOKE ALL ON FUNCTION kb.record_eval_run(jsonb, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.record_eval_run(jsonb, jsonb) TO kb_pipeline;
GRANT SELECT ON kb.eval_run, kb.eval_result TO kb_app, kb_pipeline, kb_auditor;
