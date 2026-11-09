-- 008_append_only_message: 007 reused kb.tg_audit_append_only(), whose refusal names audit_log whatever
-- the table. Same rule (no UPDATE / DELETE), honest message naming the actual table.

CREATE OR REPLACE FUNCTION kb.tg_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% refused)', TG_TABLE_NAME, TG_OP USING ERRCODE = '42501';
END $$;

DROP TRIGGER answer_feedback_append_only ON kb.answer_feedback;
CREATE TRIGGER answer_feedback_append_only BEFORE UPDATE OR DELETE ON kb.answer_feedback
  FOR EACH ROW EXECUTE FUNCTION kb.tg_append_only();
DROP TRIGGER inspection_label_append_only ON kb.inspection_label;
CREATE TRIGGER inspection_label_append_only BEFORE UPDATE OR DELETE ON kb.inspection_label
  FOR EACH ROW EXECUTE FUNCTION kb.tg_append_only();
