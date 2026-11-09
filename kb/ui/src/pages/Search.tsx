import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useSession } from "../session";
import { Head } from "../parts";

interface Hit {
  chunk_id: string;
  doc_id: string;
  version_no: number | null;
  title: string | null;
  bu: string;
  section: string | null;
  page: number | null;
  snippet: string;
  score: number;
}

export default function Search() {
  const { me } = useSession();
  const [q, setQ] = useState("");
  const [space, setSpace] = useState<string>("all");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run(e: FormEvent) {
    e.preventDefault();
    if (q.trim().length < 2) return;
    setBusy(true);
    setErr(null);
    try {
      const body = { query: q.trim(), spaces: space === "all" ? null : [space] };
      setHits((await api.post<{ results: Hit[] }>("/explorer/search", body)).results);
    } catch (x) {
      setErr((x as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Head eyebrow="Explorer" title="Search everything">
        Passages from documents you can access, ranked by the same hybrid retrieval as chat (keyword + vector + rerank), without
        an LLM answer. Every search is audited.
      </Head>
      <form className="row" onSubmit={run} style={{ marginBottom: 12 }}>
        <input
          className="grow"
          data-testid="search-input"
          placeholder="e.g. loan-to-value limits"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <div className="seg" role="group" aria-label="Space">
          {["all", ...(me?.spaces ?? [])].map((s) => (
            <button type="button" key={s} className={space === s ? "on" : ""} onClick={() => setSpace(s)}>
              {s === "all" ? "All my spaces" : s}
            </button>
          ))}
        </div>
        <button className="primary" disabled={busy}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {err && <div className="error">{err}</div>}
      {hits && (
        <div className="panel" data-testid="search-results">
          {hits.length === 0 && <p className="muted">No passages in the spaces you can access.</p>}
          {hits.map((h) => (
            <div className="result" key={h.chunk_id}>
              <div className="row">
                <Link className="title" to={`/documents/${h.doc_id}`}>
                  {h.title ?? h.doc_id}
                </Link>
                <span className="chip">{h.bu}</span>
                <span className="muted">
                  v{h.version_no ?? "?"}
                  {h.section ? ` · ${h.section}` : ""}
                  {h.page ? ` · p.${h.page}` : ""}
                </span>
                <span className="grow" />
                <span className="muted mono">{h.score.toFixed(3)}</span>
              </div>
              <div className="snip">{h.snippet}</div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
