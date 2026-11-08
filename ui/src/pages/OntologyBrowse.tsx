import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Head, Loading } from "../parts";

// Ontology browser: the pinned FIBO release (plus the local kb: vocabulary) as a module -> ontology -> class tree,
// with the caller's own BU entities mapped to each class. Same data as the star map (/ontology/fibo + overlay).
interface FNode {
  k: "M" | "O" | "C" | "P" | "K";
  n: string;
  l: string;
  m: string;
  iri?: string;
  d?: string;
}
interface Fibo {
  release: string;
  counts: Record<string, number>;
  nodes: FNode[];
  edges: [number, number, number][];
}
interface Entity {
  iri: string;
  label: string;
  bu: string;
  concept: string | null;
  match: string;
}
const CONTAINS = 0;
const SUBCLASS = 1;
const BROADER = 6;
const KIND: Record<string, string> = { M: "Module", O: "Ontology", C: "Class", P: "Property", K: "Local concept" };

export default function OntologyBrowse() {
  const [fibo, setFibo] = useState<Fibo | null>(null);
  const [ents, setEnts] = useState<Entity[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [sel, setSel] = useState<number | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    Promise.all([api.get<Fibo>("/ontology/fibo"), api.get<{ entities: Entity[] }>("/ontology/overlay")])
      .then(([f, o]) => {
        setFibo(f);
        setEnts(o.entities);
      })
      .catch((e: Error) => setErr(e.message));
  }, []);

  const idx = useMemo(() => {
    const kids = new Map<number, number[]>();
    const parents = new Map<number, number[]>();
    const subs = new Map<number, number[]>();
    for (const [a, b, k] of fibo?.edges ?? []) {
      if (k === CONTAINS) kids.set(a, [...(kids.get(a) ?? []), b]);
      if (k === SUBCLASS || k === BROADER) {
        parents.set(a, [...(parents.get(a) ?? []), b]);
        subs.set(b, [...(subs.get(b) ?? []), a]);
      }
    }
    const byIri = new Map<string, number>();
    fibo?.nodes.forEach((n, i) => n.iri && byIri.set(n.iri, i));
    const used = new Map<number, Entity[]>();
    for (const e of ents) {
      const i = e.concept ? byIri.get(e.concept) : undefined;
      if (i !== undefined && (e.match === "exact" || e.match === "accepted")) used.set(i, [...(used.get(i) ?? []), e]);
    }
    return { kids, parents, subs, used };
  }, [fibo, ents]);

  if (!fibo) return <Loading err={err} />;
  const name = (i: number) => fibo.nodes[i].l || fibo.nodes[i].n;
  const sortKids = (i: number) =>
    (idx.kids.get(i) ?? []).filter((c) => fibo.nodes[c].k !== "P").sort((a, b) => name(a).localeCompare(name(b)));
  const modules = fibo.nodes.map((n, i) => [n, i] as const).filter(([n]) => n.k === "M");
  const hits =
    q.trim().length >= 2
      ? fibo.nodes
          .map((n, i) => [n, i] as const)
          .filter(([n]) => n.k !== "P" && (n.l || n.n).toLowerCase().includes(q.trim().toLowerCase()))
          .sort(([a, i], [b, j]) => (idx.used.get(j)?.length ?? 0) - (idx.used.get(i)?.length ?? 0) || (a.l || a.n).length - (b.l || b.n).length)
          .slice(0, 40)
      : [];
  const toggle = (i: number) => {
    const s = new Set(open);
    if (s.has(i)) s.delete(i);
    else s.add(i);
    setOpen(s);
  };
  const node = (i: number, depth: number): JSX.Element => {
    const kids = depth < 2 ? sortKids(i) : [];
    const n = fibo.nodes[i];
    const used = idx.used.get(i)?.length ?? 0;
    return (
      <div key={i}>
        <button
          className={`node ${sel === i ? "on" : ""}`}
          onClick={() => {
            setSel(i);
            if (kids.length) toggle(i);
          }}
        >
          <span className="muted">{kids.length ? (open.has(i) ? "▾" : "▸") : "·"}</span>
          <span className="grow">{name(i)}</span>
          {n.k === "M" && <span className="muted">{(idx.kids.get(i) ?? []).length}</span>}
          {used > 0 && <span className="chip ok">{used} yours</span>}
        </button>
        {open.has(i) && kids.length > 0 && <div className="kids">{kids.map((c) => node(c, depth + 1))}</div>}
      </div>
    );
  };
  const s = sel !== null ? fibo.nodes[sel] : null;
  return (
    <>
      <Head eyebrow="Ontology & Semantic Management" title="Ontology management">
        FIBO {fibo.release} ({fibo.counts.C?.toLocaleString()} classes in {fibo.counts.O} ontologies) and {fibo.counts.K} local
        bank-governance concepts. FIBO is read-only here: it is a pinned public standard. New local concepts come in through
        review.
      </Head>
      <div className="split">
        <div className="panel tree" data-testid="onto-tree">
          <input
            placeholder="Find a class…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={{ width: "100%", marginBottom: 8 }}
            data-testid="onto-find"
          />
          {hits.length > 0
            ? hits.map(([n, i]) => (
                <button key={i} className={`node ${sel === i ? "on" : ""}`} onClick={() => setSel(i)}>
                  <span className="grow">{n.l || n.n}</span>
                  {(idx.used.get(i)?.length ?? 0) > 0 && <span className="chip ok">{idx.used.get(i)!.length} yours</span>}
                  <span className="muted">{n.m}</span>
                </button>
              ))
            : modules.map(([, i]) => node(i, 0))}
        </div>
        <div className="panel" data-testid="onto-detail">
          {!s ? (
            <p className="muted">Pick a module, ontology or class.</p>
          ) : (
            <>
              <div className="row">
                <h2 style={{ margin: 0 }}>{s.l || s.n}</h2>
                <span className="chip">{KIND[s.k]}</span>
                <span className="chip">{s.m}</span>
              </div>
              {s.iri && <p className="mono muted iri-line">{s.iri}</p>}
              <p>{s.d || <span className="muted">No definition in the release.</span>}</p>
              {(idx.parents.get(sel!) ?? []).length > 0 && (
                <>
                  <h3>Broader</h3>
                  {(idx.parents.get(sel!) ?? []).map((p) => (
                    <button key={p} className="link" style={{ marginRight: 12 }} onClick={() => setSel(p)}>
                      {name(p)}
                    </button>
                  ))}
                </>
              )}
              {(idx.subs.get(sel!) ?? []).length > 0 && (
                <>
                  <h3>Narrower ({(idx.subs.get(sel!) ?? []).length})</h3>
                  {(idx.subs.get(sel!) ?? []).slice(0, 30).map((p) => (
                    <button key={p} className="link" style={{ marginRight: 12 }} onClick={() => setSel(p)}>
                      {name(p)}
                    </button>
                  ))}
                </>
              )}
              <h3>Your BU entities mapped here</h3>
              {(idx.used.get(sel!) ?? []).length === 0 ? (
                <p className="muted">None in the BUs you can read.</p>
              ) : (
                <table>
                  <tbody>
                    {(idx.used.get(sel!) ?? []).map((e) => (
                      <tr key={e.iri}>
                        <td>{e.label}</td>
                        <td>
                          <span className="chip">{e.bu}</span>
                        </td>
                        <td>{e.match === "exact" ? "exact match" : "accepted by a checker"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p className="muted">
                See it in context on the <Link to="/ontology">graph</Link>.
              </p>
            </>
          )}
        </div>
      </div>
    </>
  );
}
