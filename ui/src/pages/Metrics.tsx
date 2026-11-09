import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Head, Loading, when } from "../parts";

// Semantic model & metrics governance (Phase 9c, D-067): makers draft metric definitions (structured JSON, compiled
// in code), checkers approve. A metric may not use a column its data product masks (refused by the database).
interface Gov {
  metrics: {
    metric_id: string;
    product_id: string;
    status: string;
    revision_no: number | null;
    definition: Def | null;
    description: string | null;
  }[];
  requests: {
    request_id: string;
    metric_id: string;
    maker_id: string;
    maker_comment: string | null;
    submitted_at: string;
    revision_no: number;
    proposed: Def;
    current: Def | null;
    description: string;
    masked_columns: string[];
    stale: boolean;
    own: boolean;
    sql: string;
  }[];
  drafts: {
    revision_id: string;
    metric_id: string;
    revision_no: number;
    definition: Def;
  }[];
  maker: boolean;
  checker: boolean;
  columns: Record<string, string[]>;
}
type Def = Record<string, unknown> & { label: string; table: string };

const EXAMPLE = JSON.stringify(
  {
    label: "average interest rate",
    synonyms: ["mean interest rate"],
    table: "mart_credit.loan",
    measure: {
      agg: "avg",
      column: "interest_rate",
      where: "status <> 'CLOSED'",
    },
    dimensions: ["product"],
    unit: "%",
  },
  null,
  2,
);

export default function Metrics() {
  const [g, setG] = useState<Gov | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [id, setId] = useState("avg_interest_rate");
  const [desc, setDesc] = useState("Average interest rate of open loans.");
  const [def, setDef] = useState(EXAMPLE);
  const load = useCallback(() => {
    api
      .get<Gov>("/semantic/governance")
      .then(setG)
      .catch((e: Error) => setErr(e.message));
  }, []);
  useEffect(load, [load]);
  const act = async (f: () => Promise<unknown>, ok: string) => {
    setErr(null);
    setMsg(null);
    try {
      await f();
      setMsg(ok);
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  if (!g) return <Loading err={err} />;
  return (
    <>
      <Head
        eyebrow="Ontology & Semantic Management"
        title="Semantic model & metrics"
      >
        Metric definitions are structured, never SQL text: an aggregate over a
        column (optionally with a condition), or a ratio of two, with the
        dimensions it may be broken down by. They are compiled in code and run
        as the asking user.
      </Head>
      {msg && (
        <div className="panel ok" data-testid="mx-msg">
          {msg}
        </div>
      )}
      {err && (
        <div className="error" data-testid="mx-error">
          {err}
        </div>
      )}

      {g.checker && (
        <div className="panel" data-testid="mx-requests">
          <h2 style={{ marginTop: 0 }}>
            Waiting for a checker ({g.requests.length})
          </h2>
          {g.requests.length === 0 && <p className="muted">Nothing pending.</p>}
          {g.requests.map((r) => (
            <div
              className="panel"
              key={r.request_id}
              data-testid={`mx-req-${r.metric_id}`}
            >
              <div className="row">
                <b>{r.proposed.label}</b>
                <span className="chip mono">{r.metric_id}</span>
                <span className="chip">
                  {r.current
                    ? `change to revision ${r.revision_no}`
                    : "new metric"}
                </span>
                {r.masked_columns.length > 0 && (
                  <span className="chip bad">
                    uses masked: {r.masked_columns.join(", ")}
                  </span>
                )}
                {r.stale && r.current && (
                  <span className="chip bad">stale</span>
                )}
                <span className="grow" />
                <span className="muted">
                  by {r.maker_id} · {when(r.submitted_at)}
                </span>
              </div>
              <p>{r.description}</p>
              <p className="mono" style={{ fontSize: 12 }}>
                {r.sql}
              </p>
              {r.own ? (
                <p className="muted">
                  Your own request: another checker must decide.
                </p>
              ) : (
                <div className="row">
                  <button
                    className="primary"
                    data-testid="mx-approve"
                    onClick={() =>
                      act(
                        () =>
                          api.post(
                            `/semantic/requests/${r.request_id}/decide`,
                            { approve: true, reason: "approved" },
                          ),
                        "Metric published.",
                      )
                    }
                  >
                    Approve
                  </button>
                  <button
                    onClick={() => {
                      const reason = window.prompt(
                        "Reason for rejecting (required)",
                      );
                      if (reason)
                        act(
                          () =>
                            api.post(
                              `/semantic/requests/${r.request_id}/decide`,
                              { approve: false, reason },
                            ),
                          "Rejected.",
                        );
                    }}
                  >
                    Reject
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {g.maker && (
        <div className="panel" data-testid="mx-new">
          <h2 style={{ marginTop: 0 }}>Draft a metric</h2>
          <div className="row" style={{ gap: 8 }}>
            <input
              value={id}
              onChange={(e) => setId(e.target.value)}
              className="mono"
              data-testid="mx-id"
            />
            <input
              value={desc}
              onChange={(e) => setDesc(e.target.value)}
              style={{ flex: 1 }}
              data-testid="mx-desc"
            />
          </div>
          <textarea
            rows={12}
            className="mono"
            style={{ width: "100%", marginTop: 8 }}
            value={def}
            onChange={(e) => setDef(e.target.value)}
            data-testid="mx-def"
          />
          <p className="muted">
            agg: sum, avg, min, max, count, count_distinct, or ratio (numerator,
            denominator, scale 1 or 100). unit: %, EXB$, count, number.
            Conditions use the table&apos;s columns only, as for row filters.
          </p>
          <button
            className="primary"
            data-testid="mx-save"
            onClick={() => {
              let parsed: unknown;
              try {
                parsed = JSON.parse(def);
              } catch {
                setErr("the definition is not valid JSON");
                return;
              }
              act(
                () =>
                  api.post("/semantic/drafts", {
                    metric_id: id,
                    description: desc,
                    definition: parsed,
                  }),
                "Draft saved.",
              );
            }}
          >
            Save draft
          </button>
          {g.drafts.map((d) => (
            <div
              key={d.revision_id}
              className="row"
              data-testid={`mx-draft-${d.metric_id}`}
              style={{ marginTop: 8 }}
            >
              <span className="grow">
                <b>{d.definition.label}</b>{" "}
                <span className="mono muted">
                  {d.metric_id} · revision {d.revision_no}
                </span>
              </span>
              <button
                className="primary"
                data-testid="mx-submit"
                onClick={() =>
                  act(
                    () =>
                      api.post(`/semantic/drafts/${d.revision_id}/submit`, {}),
                    "Submitted for a checker.",
                  )
                }
              >
                Submit
              </button>
              <button
                className="link"
                onClick={() =>
                  act(
                    () => api.del(`/semantic/drafts/${d.revision_id}`),
                    "Draft discarded.",
                  )
                }
              >
                Discard
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="panel" data-testid="mx-list">
        <h2 style={{ marginTop: 0 }}>Metrics</h2>
        <table>
          <thead>
            <tr>
              <th>Metric</th>
              <th>Table</th>
              <th>Status</th>
              <th>Revision</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {g.metrics.map((m) => (
              <tr key={m.metric_id}>
                <td>
                  <b>{m.definition?.label ?? m.metric_id}</b>{" "}
                  <span className="mono muted">{m.metric_id}</span>
                </td>
                <td className="mono">{m.product_id}</td>
                <td>
                  <span
                    className={`chip ${m.status === "PUBLISHED" ? "ok" : ""}`}
                  >
                    {m.status.toLowerCase()}
                  </span>
                </td>
                <td>{m.revision_no ?? "—"}</td>
                <td>
                  {m.status === "PUBLISHED" && (
                    <button
                      className="link"
                      onClick={() => {
                        const reason = window.prompt(
                          "Reason for retiring (audited)",
                        );
                        if (reason)
                          act(
                            () =>
                              api.post(`/semantic/${m.metric_id}/retire`, {
                                reason,
                              }),
                            "Retired; questions fall back to text-to-SQL.",
                          );
                      }}
                    >
                      Retire
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
