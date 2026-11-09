import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import { api, uploadFile, type UploadResult } from "../api";

interface Space {
  source_id: string;
  bu: string;
  display_name: string;
  max_file_bytes: number;
  allowed_extensions: string[];
}

interface Version {
  version_no: number;
  status: string;
  is_current: boolean;
  sha256: string;
  size_bytes: number;
  received_at: string;
}

interface MyFile {
  doc_id: string;
  title: string;
  source_id: string;
  visibility: "PRIVATE" | "PUBLISHED";
  status: string;
  duplicate_of_doc_id: string | null;
  current_version_no: number | null;
  versions: Version[] | null;
  exceptions: { reason: string; status: string; detail: { message?: string } }[] | null;
  pending_publish_request: string | null;
}

interface Notification {
  notification_id: string;
  kind: string;
  message: string;
  created_at: string;
}

function statusChip(s: string) {
  const cls = s === "INDEXED" ? "ok" : s === "EXCEPTION" ? "bad" : s === "SUPERSEDED" ? "" : "warn";
  return <span className={`chip ${cls}`}>{s}</span>;
}

export default function Uploads() {
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [space, setSpace] = useState<string>("");
  const [files, setFiles] = useState<MyFile[]>([]);
  const [notes, setNotes] = useState<Notification[]>([]);
  const [newVersionOf, setNewVersionOf] = useState<MyFile | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    const [f, n] = await Promise.all([
      api.get<MyFile[]>("/uploads/mine"),
      api.get<Notification[]>("/uploads/notifications"),
    ]);
    setFiles(f);
    setNotes(n);
  }, []);

  useEffect(() => {
    api.get<Space[]>("/uploads/spaces").then((s) => {
      setSpaces(s);
      if (s.length) setSpace(s[0].source_id);
    });
    refresh();
    const t = setInterval(refresh, 5000); // the upload sensor processes within a minute
    return () => clearInterval(t);
  }, [refresh]);

  async function send(file: File) {
    const target = newVersionOf ? newVersionOf.source_id : space;
    if (!target) return;
    setError(null);
    setResult(null);
    setProgress(0);
    try {
      setResult(await uploadFile(target, file, newVersionOf?.doc_id ?? null, setProgress));
      setNewVersionOf(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setProgress(null);
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    const f = e.dataTransfer.files[0];
    if (f) send(f);
  }

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const sp = spaces.find((s) => s.source_id === (newVersionOf?.source_id ?? space));
  return (
    <div>
      <h1>My files</h1>
      <div className="panel">
        {spaces.length === 0 ? (
          <p className="muted">You have no approved upload space.</p>
        ) : (
          <>
            <div className="row">
              {newVersionOf ? (
                <span>
                  Uploading a new version of <b>{newVersionOf.title}</b>{" "}
                  <button className="link" onClick={() => setNewVersionOf(null)}>
                    cancel
                  </button>
                </span>
              ) : (
                <label className="field">
                  Upload space
                  <select value={space} onChange={(e) => setSpace(e.target.value)} data-testid="space">
                    {spaces.map((s) => (
                      <option key={s.source_id} value={s.source_id}>
                        {s.display_name} ({s.bu})
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {sp && (
                <span className="muted">
                  allowed: {sp.allowed_extensions.join(", ")} · max {(sp.max_file_bytes / 1024 / 1024).toFixed(0)} MB
                </span>
              )}
            </div>
            <div
              className={`dropzone ${over ? "over" : ""}`}
              style={{ marginTop: 12 }}
              onDragOver={(e) => {
                e.preventDefault();
                setOver(true);
              }}
              onDragLeave={() => setOver(false)}
              onDrop={onDrop}
              onClick={() => input.current?.click()}
            >
              Drop a file here, or click to choose
              <input
                ref={input}
                type="file"
                hidden
                data-testid="file-input"
                onChange={(e) => e.target.files?.[0] && send(e.target.files[0])}
              />
            </div>
            {progress !== null && (
              <div className="progress" style={{ marginTop: 8 }}>
                <div style={{ width: `${Math.round(progress * 100)}%` }} />
              </div>
            )}
            {result && (
              <div className="notice" data-testid="upload-result">
                Received {result.version_id} ({result.status}
                {result.duplicate_of ? `, identical to ${result.duplicate_of}: linked, not indexed twice` : ""}). Gates:
                type and size passed here; malware scan and PII check run in the pipeline.
              </div>
            )}
          </>
        )}
        {error && <div className="error">{error}</div>}
      </div>

      <div className="panel">
        <h2>Files</h2>
        <table data-testid="my-files">
          <thead>
            <tr>
              <th>Document</th>
              <th>Space</th>
              <th>Visibility</th>
              <th>Versions</th>
              <th>Gate results</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {files.map((f) => (
              <tr key={f.doc_id} data-testid={`file-${f.title}`}>
                <td>
                  {f.current_version_no ? <Link to={`/documents/${f.doc_id}`}>{f.title}</Link> : f.title}
                  {f.duplicate_of_doc_id && <div className="muted">duplicate of {f.duplicate_of_doc_id}</div>}
                </td>
                <td>{f.source_id}</td>
                <td>
                  <span className={`chip ${f.visibility === "PUBLISHED" ? "ok" : ""}`} data-testid="visibility">
                    {f.visibility}
                  </span>
                  {f.pending_publish_request && <span className="chip warn">publish pending</span>}
                  {f.status !== "ACTIVE" && <span className="chip bad">{f.status}</span>}
                </td>
                <td>
                  {(f.versions ?? []).map((v) => (
                    <div key={v.version_no}>
                      v{v.version_no} {statusChip(v.status)} {v.is_current && <span className="chip">current</span>}
                    </div>
                  ))}
                </td>
                <td>
                  {(f.exceptions ?? []).map((e, i) => (
                    <div key={i} className="error" style={{ margin: 0 }}>
                      {e.reason}: {e.detail?.message ?? ""}
                    </div>
                  ))}
                  {!f.exceptions && f.versions?.some((v) => v.status === "INDEXED") && <span className="chip ok">passed</span>}
                </td>
                <td className="row" style={{ gap: 6 }}>
                  {f.status === "ACTIVE" && f.visibility === "PRIVATE" && !f.pending_publish_request && (
                    <button
                      onClick={() => act(() => api.gov.post(`/documents/${f.doc_id}/publish`, { comment: null }))}
                      data-testid="publish"
                    >
                      Request publish
                    </button>
                  )}
                  {f.visibility === "PUBLISHED" && (
                    <button
                      onClick={() => {
                        const reason = prompt("Why unpublish?");
                        if (reason) act(() => api.gov.post(`/documents/${f.doc_id}/unpublish`, { reason }));
                      }}
                    >
                      Unpublish
                    </button>
                  )}
                  {f.status === "ACTIVE" && <button onClick={() => setNewVersionOf(f)}>New version</button>}
                  {f.status === "ACTIVE" && (
                    <button
                      className="danger"
                      onClick={() => confirm(`Delete ${f.title}?`) && act(() => api.del(`/uploads/${f.doc_id}`))}
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {notes.length > 0 && (
        <div className="panel">
          <h2>Notifications</h2>
          {notes.slice(0, 10).map((n) => (
            <div key={n.notification_id}>
              <span className="muted">{new Date(n.created_at).toLocaleString()}</span> {n.message}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
