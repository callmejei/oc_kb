import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, label } from "../api";

interface Flag {
  feedback_id: string;
  bu: string;
  status: "OPEN" | "RESOLVED";
  user_id: string;
  problem: string;
  reason: string;
  created_at: string;
  resolved_by: string | null;
  resolution: string | null;
  versions: { version_id: string; doc_id: string; version_no: number; title: string; is_current: boolean }[] | null;
}

/** Thumbs-down on answers that cited this BU's documents (only this BU's part of each flag). */
export default function Flags() {
  const [status, setStatus] = useState<"OPEN" | "RESOLVED">("OPEN");
  const [flags, setFlags] = useState<Flag[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setFlags(await api.gov.get<Flag[]>(`/feedback?status_filter=${status}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [status]);
  useEffect(() => {
    refresh();
  }, [refresh]);

  async function resolve(f: Flag) {
    const resolution = prompt("Resolution note");
    if (!resolution) return;
    try {
      await api.gov.post(`/feedback/${f.feedback_id}/resolve`, { bu: f.bu, resolution });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div>
      <div className="row">
        <h1 className="grow">Answer flags</h1>
        <select value={status} onChange={(e) => setStatus(e.target.value as "OPEN" | "RESOLVED")}>
          <option>OPEN</option>
          <option>RESOLVED</option>
        </select>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="panel">
        {flags.length === 0 && <p className="muted">No {status.toLowerCase()} flags.</p>}
        <table data-testid="flags">
          <tbody>
            {flags.map((f) => (
              <tr key={`${f.feedback_id}-${f.bu}`} data-testid="flag">
                <td>
                  <span className="chip bad">{label(f.problem)}</span> <span className="muted">{f.bu}</span>
                  <div>“{f.reason}”</div>
                  <div className="muted">
                    {f.user_id} · {new Date(f.created_at).toLocaleString()}
                  </div>
                  {f.resolution && (
                    <div className="muted">
                      resolved by {f.resolved_by}: {f.resolution}
                    </div>
                  )}
                </td>
                <td>
                  {(f.versions ?? []).map((v) => (
                    <div key={v.version_id}>
                      <Link
                        to={`/documents/${v.doc_id}?v=${v.version_no}&feedback=${f.feedback_id}`}
                        data-testid="flag-doc"
                      >
                        {v.title}
                      </Link>{" "}
                      <span className="muted">
                        v{v.version_no}
                        {v.is_current ? "" : " (no longer current)"}
                      </span>
                    </div>
                  ))}
                </td>
                <td>{f.status === "OPEN" && <button onClick={() => resolve(f)}>Resolve</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
