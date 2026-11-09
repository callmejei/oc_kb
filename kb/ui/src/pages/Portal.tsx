import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { useSession } from "../session";
import { RequestDetail } from "./RequestDetail";

interface Source {
  source_id: string;
  bu: string;
  display_name: string;
  source_type: "SHAREPOINT" | "UPLOAD";
  status: string;
  current_revision_no: number | null;
  current_acl_groups: string[] | null;
  pending_request_id: string | null;
  sp_folder_path: string | null;
}

interface Exception {
  exception_id: string;
  bu: string;
  source_id: string;
  reason: string;
  status: string;
  external_item_id: string | null;
  detail: { message?: string };
  releasable: boolean;
  pending_request_id: string | null;
}

const blank = (bu: string) => ({
  source_id: "",
  display_name: "",
  source_type: "SHAREPOINT" as "SHAREPOINT" | "UPLOAD",
  sp_site_id: bu,
  sp_drive_id: `drive-${bu}`,
  sp_folder_path: "/KB",
  classification: "INTERNAL",
  retention_class: "R7Y",
  acl_groups: `GRP_KB_${bu.toUpperCase()}_READ`,
  allowed_extensions: "pdf, docx, pptx, xlsx",
  max_mb: 25,
});

export default function Portal() {
  const { me } = useSession();
  const bus = me!.maker_bus;
  const [bu, setBu] = useState(bus[0] ?? "");
  const [sources, setSources] = useState<Source[]>([]);
  const [excs, setExcs] = useState<Exception[]>([]);
  const [form, setForm] = useState(blank(bus[0] ?? ""));
  const [open, setOpen] = useState<string | null>(null);
  const [change, setChange] = useState<Source | null>(null);
  const [changeAcl, setChangeAcl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSources(await api.gov.get<Source[]>("/sources"));
      setExcs(await api.gov.get<Exception[]>("/exceptions"));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [refresh]);

  async function run(fn: () => Promise<string | void>) {
    setError(null);
    setNotice(null);
    try {
      const msg = await fn();
      if (msg) setNotice(msg);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function register(e: FormEvent) {
    e.preventDefault();
    run(async () => {
      const body: Record<string, unknown> = {
        source_id: form.source_id,
        bu,
        display_name: form.display_name,
        source_type: form.source_type,
        revision: {
          classification: form.classification,
          retention_class: form.retention_class,
          acl_groups: form.acl_groups.split(",").map((s) => s.trim()).filter(Boolean),
          allowed_extensions: form.allowed_extensions.split(",").map((s) => s.trim()).filter(Boolean),
          max_file_bytes: Math.round(form.max_mb * 1024 * 1024),
        },
      };
      if (form.source_type === "SHAREPOINT")
        Object.assign(body, { sp_site_id: form.sp_site_id, sp_drive_id: form.sp_drive_id, sp_folder_path: form.sp_folder_path });
      const r = await api.gov.post<{ revision: { revision_id: string } }>("/sources", body);
      const req = await api.gov.post<{ request_id: string }>(`/revisions/${r.revision.revision_id}/submit`, { comment: null });
      setOpen(req.request_id);
      setForm(blank(bu));
      return `Submitted ${form.source_id} for approval (request ${req.request_id.slice(0, 8)}). A dry run starts within a minute.`;
    });
  }

  function proposeChange(e: FormEvent) {
    e.preventDefault();
    if (!change) return;
    run(async () => {
      const acl = changeAcl.split(",").map((s) => s.trim()).filter(Boolean);
      const rev = await api.gov.post<{ revision_id: string }>(`/sources/${change.source_id}/revisions`, { acl_groups: acl });
      const req = await api.gov.post<{ request_id: string }>(`/revisions/${rev.revision_id}/submit`, { comment: "ACL change" });
      setOpen(req.request_id);
      setChange(null);
      return `Change to ${change.source_id} submitted.`;
    });
  }

  const mine = sources.filter((s) => s.bu === bu);
  return (
    <div>
      <div className="row">
        <h1 className="grow">Sources</h1>
        {bus.length > 1 && (
          <select value={bu} onChange={(e) => (setBu(e.target.value), setForm(blank(e.target.value)))}>
            {bus.map((b) => (
              <option key={b}>{b}</option>
            ))}
          </select>
        )}
      </div>
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}
      <div className="cols">
        <div className="panel">
          <h2>Register a source ({bu})</h2>
          <form onSubmit={register} className="row" style={{ alignItems: "flex-end" }} data-testid="register-form">
            <label className="field">
              Source id
              <input value={form.source_id} onChange={(e) => setForm({ ...form, source_id: e.target.value })} name="source_id" />
            </label>
            <label className="field grow">
              Display name
              <input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} name="display_name" />
            </label>
            <label className="field">
              Type
              <select
                value={form.source_type}
                onChange={(e) => setForm({ ...form, source_type: e.target.value as "SHAREPOINT" | "UPLOAD" })}
                name="source_type"
              >
                <option>SHAREPOINT</option>
                <option>UPLOAD</option>
              </select>
            </label>
            {form.source_type === "SHAREPOINT" && (
              <>
                <label className="field">
                  Site
                  <input value={form.sp_site_id} onChange={(e) => setForm({ ...form, sp_site_id: e.target.value })} />
                </label>
                <label className="field">
                  Folder
                  <input value={form.sp_folder_path} onChange={(e) => setForm({ ...form, sp_folder_path: e.target.value })} name="sp_folder_path" />
                </label>
              </>
            )}
            <label className="field">
              Classification
              <select value={form.classification} onChange={(e) => setForm({ ...form, classification: e.target.value })}>
                {["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"].map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </label>
            <label className="field">
              Retention
              <input value={form.retention_class} onChange={(e) => setForm({ ...form, retention_class: e.target.value })} size={6} />
            </label>
            <label className="field grow">
              ACL groups (comma-separated)
              <input value={form.acl_groups} onChange={(e) => setForm({ ...form, acl_groups: e.target.value })} name="acl_groups" />
            </label>
            <label className="field">
              File types
              <input value={form.allowed_extensions} onChange={(e) => setForm({ ...form, allowed_extensions: e.target.value })} />
            </label>
            <label className="field">
              Max MB
              <input type="number" value={form.max_mb} onChange={(e) => setForm({ ...form, max_mb: Number(e.target.value) })} style={{ width: 70 }} />
            </label>
            <button className="primary" type="submit" disabled={!form.source_id || !form.display_name}>
              Register and submit
            </button>
          </form>
        </div>
        <div className="panel">
          <h2>Sources in {bu}</h2>
          <table data-testid="sources">
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th>ACL</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {mine.map((s) => (
                <tr key={s.source_id} data-testid={`source-${s.source_id}`}>
                  <td>
                    <b>{s.source_id}</b> <span className="muted">{s.source_type}</span>
                    <div className="muted">{s.display_name}</div>
                  </td>
                  <td>
                    <span className={`chip ${s.status === "ACTIVE" ? "ok" : "warn"}`} data-testid="source-status">
                      {s.status}
                    </span>
                    {s.pending_request_id && (
                      <button className="link" onClick={() => setOpen(s.pending_request_id)}>
                        pending request
                      </button>
                    )}
                  </td>
                  <td>{(s.current_acl_groups ?? []).join(", ")}</td>
                  <td className="row" style={{ gap: 6 }}>
                    {s.status === "ACTIVE" && !s.pending_request_id && (
                      <button onClick={() => (setChange(s), setChangeAcl((s.current_acl_groups ?? []).join(", ")))}>Change ACL</button>
                    )}
                    {s.pending_request_id && (
                      <button
                        onClick={() =>
                          run(async () => {
                            await api.gov.post(`/requests/${s.pending_request_id}/withdraw`, { reason: "withdrawn by maker" });
                            return "Request withdrawn.";
                          })
                        }
                      >
                        Withdraw
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {change && (
            <form onSubmit={proposeChange} className="row" style={{ marginTop: 12 }}>
              <label className="field grow">
                New ACL for {change.source_id}
                <input value={changeAcl} onChange={(e) => setChangeAcl(e.target.value)} />
              </label>
              <button className="primary" type="submit">
                Submit change
              </button>
              <button type="button" onClick={() => setChange(null)}>
                Cancel
              </button>
            </form>
          )}
        </div>
      </div>
      {open && (
        <div className="panel">
          <div className="row">
            <h2 className="grow">Request {open.slice(0, 8)}</h2>
            <button onClick={() => setOpen(null)}>Close</button>
          </div>
          <RequestDetail requestId={open} />
        </div>
      )}
      <div className="panel">
        <h2>Exceptions</h2>
        {excs.filter((e) => e.bu === bu).length === 0 && <p className="muted">No open exceptions.</p>}
        <table>
          <tbody>
            {excs
              .filter((e) => e.bu === bu)
              .map((e) => (
                <tr key={e.exception_id}>
                  <td>
                    <span className="chip bad">{e.reason}</span> {e.source_id}
                  </td>
                  <td>{e.detail?.message}</td>
                  <td>
                    {e.releasable && !e.pending_request_id && (
                      <button
                        onClick={() =>
                          run(async () => {
                            await api.gov.post(`/exceptions/${e.exception_id}/release`, { comment: "please release" });
                            return "Release requested.";
                          })
                        }
                      >
                        Request release
                      </button>
                    )}
                    {e.pending_request_id && <span className="chip warn">release pending</span>}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
