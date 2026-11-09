import { Link } from "react-router-dom";
import { Head, Loading, useGet, when } from "../parts";

interface Doc {
  doc_id: string;
  title: string | null;
  bu: string;
  source_id: string;
  origin: string | null;
  version_no: number | null;
  indexed_at: string | null;
  chunks: number;
  pages: number | null;
  private: boolean;
}
interface Inventory {
  bus: string[];
  spaces: { bu: string; count: number; documents: Doc[] }[];
  other_visible: Doc[];
}

function DocTable({ docs }: { docs: Doc[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Document</th>
          <th>Source</th>
          <th>Version</th>
          <th>Visibility</th>
          <th>Pages</th>
          <th>Chunks</th>
          <th>Indexed</th>
        </tr>
      </thead>
      <tbody>
        {docs.map((d) => (
          <tr key={d.doc_id}>
            <td>
              <Link to={`/documents/${d.doc_id}`}>{d.title ?? d.doc_id}</Link>
            </td>
            <td className="mono">{d.source_id}</td>
            <td>v{d.version_no ?? "?"}</td>
            <td>{d.private ? <span className="chip warn">My files (private)</span> : <span className="chip ok">BU space</span>}</td>
            <td>{d.pages ?? "—"}</td>
            <td>{d.chunks}</td>
            <td>{when(d.indexed_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function Spaces() {
  const [inv, err] = useGet<Inventory>("/explorer/spaces/inventory");
  return (
    <>
      <Head eyebrow="Knowledge Space Management" title="Space asset inventory">
        Every current document you can open, per BU space. The search index decides with the same access filter as chat, so
        another user's private upload never appears here.
      </Head>
      {!inv ? (
        <Loading err={err} />
      ) : inv.bus.length === 0 ? (
        <div className="panel muted">You have no readable BU space.</div>
      ) : (
        <>
          {inv.spaces.map((s) => (
            <div className="panel" key={s.bu} data-testid={`space-${s.bu}`}>
              <h2 style={{ marginTop: 0 }}>
                {s.bu} <span className="muted">· {s.count} documents</span>
              </h2>
              <DocTable docs={s.documents} />
            </div>
          ))}
          {inv.other_visible.length > 0 && (
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Other documents you can see</h2>
              <DocTable docs={inv.other_visible} />
            </div>
          )}
        </>
      )}
    </>
  );
}
