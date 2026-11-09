import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";

interface Change {
  field: string;
  current: unknown;
  proposed: unknown;
}

export interface Detail {
  request: {
    request_id: string;
    request_type: string;
    status: string;
    maker_id: string;
    maker_comment: string | null;
    checker_id: string | null;
    decision_reason: string | null;
    submitted_at: string;
    doc_id: string | null;
  };
  source: { source_id: string; bu: string; display_name: string; source_type: string; status: string };
  diff?: { changes: Change[]; acl_added: string[]; acl_removed: string[] };
  dry_run?: { status: string; stats: { counts?: Record<string, number>; exceptions?: { reason: string; item: string }[] } } | null;
  document?: { doc_id: string; title: string; uploaded_by: string | null; visibility: string | null; status: string } | null;
  acl_change?: { from: string[]; to: string[] };
}

function show(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** What a checker needs to decide: proposed vs current config, ACL diff, the dry-run report. */
export function RequestDetail({ requestId, onLoaded }: { requestId: string; onLoaded?: (d: Detail) => void }) {
  const [d, setD] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.gov
        .get<Detail>(`/requests/${requestId}`)
        .then((x) => {
          if (!alive) return;
          setD(x);
          onLoaded?.(x);
        })
        .catch((e) => setError(String(e.message ?? e)));
    load();
    const t = setInterval(load, 5000); // the dry run lands within a minute of submission
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [requestId, onLoaded]);

  if (error) return <div className="error">{error}</div>;
  if (!d) return <div className="muted">Loading…</div>;
  const r = d.request;
  return (
    <div data-testid="request-detail">
      <div className="row">
        <span className="chip">{r.request_type}</span>
        <span className={`chip ${r.status === "APPROVED" ? "ok" : r.status === "REJECTED" ? "bad" : "warn"}`}>{r.status}</span>
        <span>
          {d.source.source_id} ({d.source.bu}) · maker <b>{r.maker_id}</b>
        </span>
        {r.checker_id && (
          <span>
            · checker <b>{r.checker_id}</b>
          </span>
        )}
      </div>
      {r.maker_comment && <p>“{r.maker_comment}”</p>}
      {r.decision_reason && <p className="muted">Decision: {r.decision_reason}</p>}

      {d.diff && (
        <>
          <h3>ACL change</h3>
          <div data-testid="acl-diff">
            {d.diff.acl_added.map((g) => (
              <div key={g} className="diff-add">
                + {g}
              </div>
            ))}
            {d.diff.acl_removed.map((g) => (
              <div key={g} className="diff-del">
                − {g}
              </div>
            ))}
            {d.diff.acl_added.length + d.diff.acl_removed.length === 0 && <span className="muted">no ACL change</span>}
          </div>
          <h3>Configuration: current vs proposed</h3>
          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>Current</th>
                <th>Proposed</th>
              </tr>
            </thead>
            <tbody>
              {d.diff.changes.map((c) => (
                <tr key={c.field}>
                  <td>{c.field}</td>
                  <td className="diff-del">{show(c.current)}</td>
                  <td className="diff-add">{show(c.proposed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {d.source.source_type === "SHAREPOINT" && d.diff && (
        <>
          <h3>Dry-run report</h3>
          {!d.dry_run ? (
            <p className="muted" data-testid="dry-run-pending">
              Dry run pending (runs within a minute of submission)…
            </p>
          ) : (
            <div data-testid="dry-run">
              <span className="chip">{d.dry_run.status}</span>{" "}
              {Object.entries(d.dry_run.stats.counts ?? {}).map(([k, v]) => (
                <span key={k} className="chip">
                  {k}: {v}
                </span>
              ))}
              {(d.dry_run.stats.exceptions ?? []).map((e, i) => (
                <div key={i} className="muted">
                  would except: {e.reason} {e.item}
                </div>
              ))}
            </div>
          )}
        </>
      )}
      {d.document && (
        <>
          <h3>Document</h3>
          <p>
            <Link to={`/documents/${d.document.doc_id}`}>{d.document.title}</Link>{" "}
            <span className="muted">
              {d.document.status} · {d.document.visibility ?? ""} · uploaded by {d.document.uploaded_by ?? "SharePoint"}
            </span>
          </p>
          {d.acl_change && (
            <div data-testid="acl-diff">
              {d.acl_change.from.map((g) => (
                <div key={g} className="diff-del">
                  − {g}
                </div>
              ))}
              {d.acl_change.to.map((g) => (
                <div key={g} className="diff-add">
                  + {g}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
