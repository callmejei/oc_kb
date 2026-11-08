import { Link } from "react-router-dom";
import { label } from "../api";
import { Head, Loading, useGet, when } from "../parts";

interface Req {
  request_id: string;
  request_type: string;
  source_id: string;
  bu: string;
  display_name: string;
  maker_id: string;
  submitted_at: string;
  status: string;
  checker_id: string | null;
  decided_at: string | null;
  decision_reason: string | null;
}

const TONE: Record<string, string> = { APPROVED: "ok", PENDING: "warn", REJECTED: "bad" };

export default function Approvals() {
  const [data, err] = useGet<{ bus: string[]; requests: Req[] }>("/explorer/governance/approvals?limit=200");
  return (
    <>
      <Head eyebrow="Data/Knowledge Governance" title="Governance & security: approval history">
        Every maker-checker request on sources in the BUs where you are a maker or checker. The database enforces four eyes (the
        maker can never be the checker); this page only shows the record.
      </Head>
      {!data ? (
        <Loading err={err} />
      ) : (
        <div className="panel">
          <p className="muted">BUs: {data.bus.join(", ")}</p>
          <table data-testid="approvals">
            <thead>
              <tr>
                <th>Submitted</th>
                <th>Type</th>
                <th>Source</th>
                <th>Maker</th>
                <th>Status</th>
                <th>Checker</th>
                <th>Decided</th>
              </tr>
            </thead>
            <tbody>
              {data.requests.map((r) => (
                <tr key={r.request_id}>
                  <td>{when(r.submitted_at)}</td>
                  <td>{label(r.request_type)}</td>
                  <td>
                    {r.display_name} <span className="chip">{r.bu}</span>
                  </td>
                  <td className="mono">{r.maker_id}</td>
                  <td>
                    <span className={`chip ${TONE[r.status] ?? ""}`}>{label(r.status)}</span>
                  </td>
                  <td className="mono">{r.checker_id ?? "—"}</td>
                  <td title={r.decision_reason ?? ""}>{when(r.decided_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ marginBottom: 0 }}>
            Pending requests are decided in the <Link to="/inbox">checker inbox</Link>.
          </p>
        </div>
      )}
    </>
  );
}
