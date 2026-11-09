import { Head, Loading, useGet, when } from "../parts";

// Data & knowledge quality (Phase 9a, D-065). Data rules: counts only, for tables you are granted (ops: all).
// Knowledge: corpus health for the BUs you can read (ops: all). Shown, never used to block answers.
interface Rule {
  rule_id: string;
  table_name: string;
  rule_kind: string;
  column_name: string;
  checked: number;
  failed: number;
  tolerance: number;
  passed: boolean;
  error: string | null;
}
interface Know {
  bu: string;
  current_documents: number;
  stale_documents: number;
  open_exceptions: Record<string, number>;
  labelled_documents: number;
  concepts_pending: number;
  concepts_accepted: number;
}
interface Q {
  data: { run: { run_id: string; finished_at: string; as_of: string } | null; rules: Rule[]; tables_not_shown: number | null };
  knowledge: Know[];
  scope: "all" | "own";
  stale_days: number;
}

const KIND: Record<string, string> = {
  not_null: "no blanks",
  range: "within range",
  unique: "unique",
  allowed_values: "allowed values",
  max_age: "recent enough",
  reference: "valid reference",
};

const pct = (n: number, d: number) => (d ? `${((100 * n) / d).toFixed(n && n / d < 0.001 ? 2 : 1)}%` : "—");

export default function Quality() {
  const [q, err] = useGet<Q>("/quality");
  if (!q) return <Loading err={err} />;
  const failing = q.data.rules.filter((r) => !r.passed);
  const byTable = new Map<string, Rule[]>();
  for (const r of q.data.rules) byTable.set(r.table_name, [...(byTable.get(r.table_name) ?? []), r]);
  return (
    <>
      <Head eyebrow="Quality & Observability" title="Data & knowledge quality">
        Rule checks on the marts (counts of rows checked and failing, never the rows themselves) and the health of the
        document corpus. {q.scope === "all" ? "You see every table and BU (operations)." : "Scoped to the tables you are granted and the BUs you can read."}{" "}
        Results are shown for information; they do not block answers.
      </Head>

      <div className="panel" data-testid="dq-data">
        <div className="row">
          <h2 style={{ margin: 0 }}>Data rules</h2>
          {q.data.run && (
            <span className="muted">
              last run {when(q.data.run.finished_at)} · as of {q.data.run.as_of}
            </span>
          )}
          <span className="grow" />
          {q.data.rules.length > 0 && (
            <span className={`chip ${failing.length ? "bad" : "ok"}`} data-testid="dq-summary">
              {q.data.rules.length - failing.length} of {q.data.rules.length} passing
            </span>
          )}
        </div>
        {!q.data.run || q.data.rules.length === 0 ? (
          <p className="muted">{q.data.tables_not_shown === null ? "No data-quality run recorded yet." : "No rules on tables you are granted."}</p>
        ) : (
          [...byTable.entries()].map(([t, rs]) => (
            <div key={t} data-testid={`dq-table-${t}`}>
              <h3 className="mono">{t}</h3>
              <table>
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th>Column</th>
                    <th>Check</th>
                    <th style={{ textAlign: "right" }}>Rows checked</th>
                    <th style={{ textAlign: "right" }}>Failing</th>
                    <th style={{ textAlign: "right" }}>Tolerance</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {rs.map((r) => (
                    <tr key={r.rule_id} className={r.passed ? "" : "bad"}>
                      <td className="mono">{r.rule_id}</td>
                      <td className="mono">{r.column_name}</td>
                      <td>{KIND[r.rule_kind] ?? r.rule_kind}</td>
                      <td style={{ textAlign: "right" }}>{r.checked.toLocaleString()}</td>
                      <td style={{ textAlign: "right" }}>
                        {r.failed.toLocaleString()} <span className="muted">({pct(r.failed, r.checked)})</span>
                      </td>
                      <td style={{ textAlign: "right" }}>{pct(Number(r.tolerance), 1)}</td>
                      <td>
                        {r.error ? (
                          <span className="chip bad" title={r.error}>
                            could not run
                          </span>
                        ) : r.passed ? (
                          <span className="chip ok">✓ pass</span>
                        ) : (
                          <span className="chip bad">✕ fail</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))
        )}
        {!!q.data.tables_not_shown && (
          <p className="muted">
            {q.data.tables_not_shown} other table{q.data.tables_not_shown > 1 ? "s are" : " is"} checked but not shown: you are
            not granted {q.data.tables_not_shown > 1 ? "them" : "it"}.
          </p>
        )}
      </div>

      <div className="panel" data-testid="dq-knowledge">
        <h2 style={{ marginTop: 0 }}>Knowledge corpus</h2>
        {q.knowledge.length === 0 ? (
          <p className="muted">No BU spaces you can read.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>BU</th>
                <th style={{ textAlign: "right" }}>Current documents</th>
                <th style={{ textAlign: "right" }}>Stale (&gt; {q.stale_days} days)</th>
                <th>Open exceptions</th>
                <th style={{ textAlign: "right" }}>Documents with labelled problems</th>
                <th style={{ textAlign: "right" }}>Ontology: accepted / awaiting review</th>
              </tr>
            </thead>
            <tbody>
              {q.knowledge.map((k) => {
                const ex = Object.entries(k.open_exceptions);
                return (
                  <tr key={k.bu} data-testid={`dq-bu-${k.bu}`}>
                    <td>
                      <span className="chip">{k.bu}</span>
                    </td>
                    <td style={{ textAlign: "right" }}>{k.current_documents}</td>
                    <td style={{ textAlign: "right" }}>{k.stale_documents}</td>
                    <td>
                      {ex.length === 0 ? (
                        <span className="muted">none</span>
                      ) : (
                        ex.map(([reason, n]) => (
                          <span key={reason} className="chip warn" style={{ marginRight: 4 }}>
                            {reason.toLowerCase().replace(/_/g, " ")}: {n}
                          </span>
                        ))
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>{k.labelled_documents}</td>
                    <td style={{ textAlign: "right" }}>
                      {k.concepts_accepted} / {k.concepts_pending}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
