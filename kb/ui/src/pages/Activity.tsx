import { Head, Loading, useGet, when } from "../parts";

interface Row {
  audit_id: number;
  ts: string;
  action: string;
  object_type: string;
  object_id: string | null;
}

export default function Activity() {
  const [rows, err] = useGet<Row[]>("/explorer/governance/activity?limit=200");
  return (
    <>
      <Head eyebrow="Data/Knowledge Governance" title="Audit & lineage: your activity">
        Your own entries in the append-only audit log (questions, searches, uploads, governance actions). Question text is kept
        apart and is readable by auditors only. Full audit search across users is an auditor function and is not exposed here.
      </Head>
      {!rows ? (
        <Loading err={err} />
      ) : (
        <div className="panel">
          <table data-testid="activity">
            <thead>
              <tr>
                <th>#</th>
                <th>When</th>
                <th>Action</th>
                <th>Object</th>
                <th>Id</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.audit_id}>
                  <td className="mono">{r.audit_id}</td>
                  <td>{when(r.ts)}</td>
                  <td>
                    <span className="chip">{r.action}</span>
                  </td>
                  <td>{r.object_type}</td>
                  <td className="mono">{r.object_id ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="muted">No activity yet.</p>}
        </div>
      )}
    </>
  );
}
