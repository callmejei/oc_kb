import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, label, PROBLEMS, type Problem } from "../api";
import PdfPage, { type Region } from "../components/PdfPage";

interface Version {
  version_no: number;
  version_id: string;
  status: string;
  is_current: boolean;
  chunk_count: number | null;
  mime_type: string | null;
  original_filename: string | null;
  parse_options: Record<string, string> | null;
  reparse_of: string | null;
  indexed_at: string | null;
}

interface DocResponse {
  document: {
    doc_id: string;
    title: string;
    current_path: string;
    status: string;
    origin: string;
    visibility: string | null;
    bu: string;
    source_id: string;
    source_name: string;
  };
  versions: Version[];
  governance: {
    governs: boolean;
    is_maker: boolean;
    labels: { label_id: string; version_id: string; page: number | null; item_ref: string | null; problem: string; note: string | null; created_by: string; created_at: string }[] | null;
    reparses: { reparse_id: string; status: string; options: Record<string, string>; reason: string; requested_by: string; result_version_id: string | null; error: string | null; created_at: string }[] | null;
    pending_include_request: string | null;
  };
}

interface Block {
  ref: string;
  label: string;
  level: number;
  text: string;
  boxes: { page: number; bbox: [number, number, number, number] }[];
}

interface Chunk {
  chunk_id: string;
  chunk_no: number;
  text: string;
  page: number | null;
  page_end: number | null;
  section_path: string | null;
  doc_items: { ref: string; label: string; page?: number }[];
}

export default function Inspector() {
  const { docId } = useParams();
  const [qs] = useSearchParams();
  const [doc, setDoc] = useState<DocResponse | null>(null);
  const [vno, setVno] = useState<number | null>(qs.get("v") ? Number(qs.get("v")) : null);
  const [blocks, setBlocks] = useState<{ pages: Record<string, { width: number; height: number }>; blocks: Block[] } | null>(null);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [pdf, setPdf] = useState<ArrayBuffer | null>(null);
  const [page, setPage] = useState<number>(Number(qs.get("page")) || 1);
  const [pages, setPages] = useState(0);
  const [active, setActive] = useState<string | null>(null);
  const [activeChunk, setActiveChunk] = useState<string | null>(qs.get("chunk") || null);
  const [tab, setTab] = useState<"blocks" | "chunks">(qs.get("chunk") ? "chunks" : "blocks");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const feedbackId = qs.get("feedback");

  const loadDoc = useCallback(async () => {
    try {
      const d = await api.get<DocResponse>(`/documents/${docId}`);
      setDoc(d);
      setVno((cur) => {
        if (cur && d.versions.some((v) => v.version_no === cur)) return cur;
        return (d.versions.find((v) => v.is_current) ?? d.versions[d.versions.length - 1])?.version_no ?? null;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [docId]);

  useEffect(() => {
    loadDoc();
  }, [loadDoc]);

  // poll while a re-parse is queued
  useEffect(() => {
    if (!doc?.governance.reparses?.some((r) => r.status === "QUEUED")) return;
    const t = setInterval(loadDoc, 4000);
    return () => clearInterval(t);
  }, [doc, loadDoc]);

  const version = doc?.versions.find((v) => v.version_no === vno) ?? null;
  const isPdf = !!version && (version.mime_type === "pdf" || /\.pdf$/i.test(version.original_filename ?? doc?.document.current_path ?? ""));

  useEffect(() => {
    if (!vno) return;
    setBlocks(null);
    setChunks([]);
    setPdf(null);
    api.get<typeof blocks>(`/documents/${docId}/versions/${vno}/blocks`).then(setBlocks).catch((e) => setError(e.message));
    api.get<Chunk[]>(`/documents/${docId}/versions/${vno}/chunks`).then(setChunks).catch((e) => setError(e.message));
  }, [docId, vno]);

  useEffect(() => {
    if (vno && isPdf) api.bytes(`/documents/${docId}/versions/${vno}/file`).then(setPdf).catch((e) => setError(e.message));
  }, [docId, vno, isPdf]);

  const chunkRefs = useMemo(() => {
    const c = chunks.find((x) => x.chunk_id === activeChunk);
    return new Set(c ? c.doc_items.map((i) => i.ref) : []);
  }, [chunks, activeChunk]);

  const regions: Region[] = useMemo(
    () =>
      (blocks?.blocks ?? []).flatMap((b) =>
        b.boxes.filter((x) => x.page === page).map((x) => ({ ref: b.ref, label: b.label, bbox: x.bbox })),
      ),
    [blocks, page],
  );

  function pick(ref: string) {
    setActive(ref);
    setTab("blocks");
    document.getElementById(`blk-${ref}`)?.scrollIntoView({ block: "nearest" });
  }

  function pickBlock(b: Block) {
    setActive(b.ref);
    if (b.boxes[0]) setPage(b.boxes[0].page);
  }

  function pickChunk(c: Chunk) {
    setActiveChunk(c.chunk_id);
    if (c.page) setPage(c.page);
  }

  async function run(fn: () => Promise<string>) {
    setError(null);
    setNotice(null);
    try {
      setNotice(await fn());
      await loadDoc();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (error && !doc) return <div className="error">{error}</div>;
  if (!doc) return <div className="muted">Loading…</div>;
  const d = doc.document;
  return (
    <div>
      <div className="row">
        <h1 className="grow" data-testid="doc-title">
          {d.title}
        </h1>
        <span className={`chip ${d.status === "ACTIVE" ? "ok" : "bad"}`} data-testid="doc-status">
          {d.status}
        </span>
        <span className="muted">
          {d.bu} · {d.source_name}
        </span>
        <label className="row" style={{ gap: 4 }}>
          Version
          <select value={vno ?? ""} onChange={(e) => setVno(Number(e.target.value))} data-testid="version">
            {doc.versions.map((v) => (
              <option key={v.version_no} value={v.version_no}>
                v{v.version_no} {v.is_current ? "(current)" : `(${v.status.toLowerCase()})`}
              </option>
            ))}
          </select>
        </label>
      </div>
      {version && (
        <div className="muted" style={{ marginBottom: 8 }}>
          {version.chunk_count ?? 0} chunks
          {version.reparse_of && ` · re-parse of ${version.reparse_of.split(":").pop()}`}
          {version.parse_options && Object.keys(version.parse_options).length > 0 &&
            ` · options ${Object.entries(version.parse_options).map(([k, v]) => `${k}=${v}`).join(", ")}`}
        </div>
      )}
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      <div className="inspector">
        <div className="panel">
          {isPdf ? (
            <>
              <div className="row" style={{ marginBottom: 8 }}>
                <button disabled={page <= 1} onClick={() => setPage(page - 1)}>
                  ‹ Prev
                </button>
                <span data-testid="page-no">
                  Page {page} / {pages || "…"}
                </span>
                <button disabled={pages > 0 && page >= pages} onClick={() => setPage(page + 1)}>
                  Next ›
                </button>
                <span className="muted">{regions.length} regions</span>
              </div>
              {pdf ? (
                <PdfPage data={pdf} page={page} regions={regions} active={active} onPick={pick} onPages={setPages} />
              ) : (
                <div className="muted">Loading page…</div>
              )}
            </>
          ) : (
            <p className="muted" data-testid="no-page-view">
              No page image for {version?.original_filename?.split(".").pop()?.toUpperCase() ?? "Office"} files: Docling
              keeps their structure (headings, paragraphs, tables) but not page geometry. Blocks and chunks are on the right.
            </p>
          )}
        </div>
        <div className="panel">
          <div className="tabs">
            <button className={tab === "blocks" ? "active" : ""} onClick={() => setTab("blocks")}>
              Extracted blocks ({blocks?.blocks.length ?? 0})
            </button>
            <button className={tab === "chunks" ? "active" : ""} onClick={() => setTab("chunks")}>
              Chunks ({chunks.length})
            </button>
          </div>
          <div className="blocks">
            {tab === "blocks" &&
              (blocks?.blocks ?? []).map((b) => (
                <div
                  key={b.ref}
                  id={`blk-${b.ref}`}
                  className={`block ${active === b.ref || chunkRefs.has(b.ref) ? "active" : ""}`}
                  onClick={() => pickBlock(b)}
                  data-testid="block"
                >
                  <div className="lbl">
                    {b.label} {b.boxes[0] ? `· p.${b.boxes[0].page}` : ""}
                  </div>
                  <pre>{b.text || <span className="muted">(no text)</span>}</pre>
                </div>
              ))}
            {tab === "chunks" &&
              chunks.map((c) => (
                <div
                  key={c.chunk_id}
                  className="chunk"
                  style={activeChunk === c.chunk_id ? { borderColor: "var(--bad)" } : undefined}
                  onClick={() => pickChunk(c)}
                  data-testid="chunk"
                >
                  <div className="muted">
                    #{c.chunk_no} {c.section_path ? `· ${c.section_path}` : ""} {c.page ? `· p.${c.page}` : ""}
                  </div>
                  {c.text}
                </div>
              ))}
          </div>
        </div>
      </div>

      {doc.governance.governs && version && (
        <Governance doc={doc} version={version} page={isPdf ? page : null} itemRef={active} feedbackId={feedbackId} run={run} />
      )}
    </div>
  );
}

function Governance({
  doc,
  version,
  page,
  itemRef,
  feedbackId,
  run,
}: {
  doc: DocResponse;
  version: Version;
  page: number | null;
  itemRef: string | null;
  feedbackId: string | null;
  run: (fn: () => Promise<string>) => void;
}) {
  const g = doc.governance;
  const d = doc.document;
  const [problem, setProblem] = useState<Problem>("TABLE_BROKEN");
  const [note, setNote] = useState("");
  const [ocr, setOcr] = useState<"default" | "full_page">("default");
  const [tableMode, setTableMode] = useState<"accurate" | "fast">("accurate");
  const [cells, setCells] = useState(true);
  const [reason, setReason] = useState("");

  return (
    <div className="panel" data-testid="governance">
      <h2>Problems and actions</h2>
      <div className="cols">
        <div>
          <h3>Label a problem</h3>
          <div className="row">
            <select value={problem} onChange={(e) => setProblem(e.target.value as Problem)} data-testid="label-problem">
              {PROBLEMS.map((p) => (
                <option key={p} value={p}>
                  {label(p)}
                </option>
              ))}
            </select>
            <input className="grow" placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} data-testid="label-note" />
            <button
              data-testid="add-label"
              onClick={() =>
                run(async () => {
                  await api.post(`/documents/${d.doc_id}/labels`, {
                    version_no: version.version_no,
                    page,
                    item_ref: itemRef,
                    problem,
                    note: note || null,
                    feedback_id: feedbackId,
                  });
                  setNote("");
                  return `Labelled ${label(problem)}${itemRef ? ` on ${itemRef}` : ""}${page ? ` (p.${page})` : ""}.`;
                })
              }
            >
              Add label
            </button>
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {itemRef ? `Selected region ${itemRef}` : "Click a region or block to attach the label to it."}
          </div>
          <table style={{ marginTop: 8 }} data-testid="labels">
            <tbody>
              {(g.labels ?? []).map((l) => (
                <tr key={l.label_id}>
                  <td>
                    <span className="chip bad">{label(l.problem)}</span> {l.version_id.split(":").pop()}
                    {l.page ? ` p.${l.page}` : ""} {l.item_ref ?? ""}
                  </td>
                  <td>{l.note}</td>
                  <td className="muted">{l.created_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          {d.status === "ACTIVE" && g.is_maker && (
            <>
              <h3>Re-parse</h3>
              <div className="row">
                <label className="field">
                  OCR
                  <select value={ocr} onChange={(e) => setOcr(e.target.value as "default" | "full_page")} data-testid="opt-ocr">
                    <option value="default">default</option>
                    <option value="full_page">full page</option>
                  </select>
                </label>
                <label className="field">
                  Tables
                  <select value={tableMode} onChange={(e) => setTableMode(e.target.value as "accurate" | "fast")}>
                    <option value="accurate">accurate</option>
                    <option value="fast">fast</option>
                  </select>
                </label>
                <label className="row" style={{ gap: 4 }}>
                  <input type="checkbox" checked={cells} onChange={(e) => setCells(e.target.checked)} data-testid="opt-cells" />
                  match table cells to PDF text
                </label>
              </div>
            </>
          )}
          {d.status === "ACTIVE" && (
            <label className="field" style={{ marginTop: 8 }}>
              Reason
              <input value={reason} onChange={(e) => setReason(e.target.value)} data-testid="action-reason" />
            </label>
          )}
          <div className="row" style={{ marginTop: 8 }}>
            {d.status === "ACTIVE" && g.is_maker && (
              <button
                className="primary"
                disabled={!reason.trim() || !!g.reparses?.some((r) => r.status === "QUEUED")}
                data-testid="reparse"
                onClick={() =>
                  run(async () => {
                    const r = await api.post<{ reparse_id: string }>(`/documents/${d.doc_id}/reparse`, {
                      options: { ocr_mode: ocr, table_mode: tableMode, table_cell_matching: cells },
                      reason,
                      feedback_id: feedbackId,
                    });
                    setReason("");
                    return `Re-parse queued (${r.reparse_id.slice(0, 8)}). The pipeline picks it up within a minute.`;
                  })
                }
              >
                Re-parse
              </button>
            )}
            {d.status === "ACTIVE" && (
              <button
                className="danger"
                disabled={!reason.trim()}
                data-testid="exclude"
                onClick={() =>
                  confirm("Exclude this document from search? Re-including it needs a checker.") &&
                  run(async () => {
                    await api.post(`/documents/${d.doc_id}/exclude`, { reason });
                    setReason("");
                    return "Excluded: removed from search now.";
                  })
                }
              >
                Exclude from search
              </button>
            )}
            {d.status === "EXCLUDED" && g.is_maker && !g.pending_include_request && (
              <button
                data-testid="include"
                onClick={() =>
                  run(async () => {
                    await api.post(`/documents/${d.doc_id}/include`, { comment: "re-include" });
                    return "Re-include requested: a checker must approve.";
                  })
                }
              >
                Request re-include
              </button>
            )}
            {g.pending_include_request && <span className="chip warn">re-include pending checker</span>}
          </div>
          <h3>Re-parse history</h3>
          <table data-testid="reparses">
            <tbody>
              {(g.reparses ?? []).map((r) => (
                <tr key={r.reparse_id}>
                  <td>
                    <span className={`chip ${r.status === "DONE" ? "ok" : r.status === "FAILED" ? "bad" : "warn"}`} data-testid="reparse-status">
                      {r.status}
                    </span>
                  </td>
                  <td>
                    {Object.entries(r.options).map(([k, v]) => `${k}=${v}`).join(", ") || "defaults"}
                    <div className="muted">{r.reason}</div>
                  </td>
                  <td className="muted">
                    {r.result_version_id ? `→ ${r.result_version_id.split(":").pop()}` : r.error ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
