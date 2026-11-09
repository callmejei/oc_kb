import { Head, Loading, useGet } from "../parts";

// Semantic products (Phase 9d, D-067): approved metrics on tables you are granted, with lineage and data quality.
// Read-only. Asking in chat uses the same governed definition, computed for you (your row filter applies).
interface M {
  metric_id: string;
  label: string;
  synonyms: string[];
  description: string;
  unit: string;
  dimensions: string[];
  concept: string | null;
  revision: number;
  sql: string;
  lineage: {
    table: string;
    columns: string[];
    product_revision: number;
    owner: string;
    classification: string;
  };
  quality: { passed: number; rules: number } | null;
  usable: boolean;
}

export default function SemanticCatalog() {
  const [data, err] = useGet<{ metrics: M[]; not_shown: number }>(
    "/semantic/metrics",
  );
  return (
    <>
      <Head eyebrow="Enterprise Marketplace" title="Semantic products">
        Governed business metrics. Each is defined once, approved by a checker,
        and computed in code for whoever asks, so your own row filter and masks
        apply. Ask for one in chat by name, optionally &ldquo;by&rdquo; a listed
        dimension.
      </Head>
      {!data ? (
        <Loading err={err} />
      ) : (
        <>
          {data.metrics.length === 0 && (
            <div className="panel muted">
              No metrics on tables you are granted.
            </div>
          )}
          {data.metrics.map((m) => (
            <div
              className="panel"
              key={m.metric_id}
              data-testid={`metric-${m.metric_id}`}
            >
              <div className="row">
                <h2 style={{ margin: 0 }}>{m.label}</h2>
                <span className="chip">{m.unit}</span>
                <span className="chip">revision {m.revision}</span>
                {!m.usable && (
                  <span className="chip bad">
                    uses a column masked for you: not available to you
                  </span>
                )}
                <span className="grow" />
                {m.quality && (
                  <span
                    className={`chip ${m.quality.passed === m.quality.rules ? "ok" : "warn"}`}
                  >
                    data quality {m.quality.passed}/{m.quality.rules}
                  </span>
                )}
              </div>
              <p>{m.description}</p>
              <table>
                <tbody>
                  <tr>
                    <th>Ask as</th>
                    <td>
                      {[m.label, ...m.synonyms].map((s) => `“${s}”`).join(", ")}
                    </td>
                  </tr>
                  <tr>
                    <th>Break down by</th>
                    <td>
                      {m.dimensions.length ? (
                        m.dimensions.join(", ")
                      ) : (
                        <span className="muted">none</span>
                      )}
                    </td>
                  </tr>
                  <tr>
                    <th>Lineage</th>
                    <td>
                      <span className="mono">{m.lineage.table}</span> (
                      {m.lineage.columns.join(", ")}) · data product revision{" "}
                      {m.lineage.product_revision} · owner {m.lineage.owner} ·{" "}
                      {m.lineage.classification.toLowerCase()}
                    </td>
                  </tr>
                  <tr>
                    <th>Definition</th>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {m.sql}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          ))}
          {data.not_shown > 0 && (
            <p className="muted">
              {data.not_shown} other metric
              {data.not_shown > 1 ? "s exist" : " exists"} on tables you are not
              granted.
            </p>
          )}
        </>
      )}
    </>
  );
}
