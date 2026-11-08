import { Link } from "react-router-dom";
import { Head, Loading, useGet } from "../parts";

interface G {
  bu: string;
  entities: number;
  chunks: number;
  exact?: number;
  accepted?: number;
  pending?: number;
  unmapped?: number;
}

export default function Graphs() {
  const [data, err] = useGet<{ bus: string[]; graphs: G[] }>("/explorer/catalog/graphs");
  return (
    <>
      <Head eyebrow="Ontology & Semantic Management" title="Knowledge graph management">
        One named graph per BU, built from that BU's documents only. You see the graphs of the BUs you can read. "Mapped" means
        linked to a FIBO or local concept (exact match, or accepted by a checker).
      </Head>
      {!data ? (
        <Loading err={err} />
      ) : data.graphs.length === 0 ? (
        <div className="panel muted">You can read no BU graph.</div>
      ) : (
        <div className="panel">
          <table data-testid="graphs">
            <thead>
              <tr>
                <th>Graph</th>
                <th>Entities</th>
                <th>Exact</th>
                <th>Accepted</th>
                <th>Pending review</th>
                <th>Unmapped</th>
                <th>Mapped share</th>
                <th>Chunk links</th>
              </tr>
            </thead>
            <tbody>
              {data.graphs.map((g) => {
                const mapped = (g.exact ?? 0) + (g.accepted ?? 0);
                const share = g.entities ? mapped / g.entities : 0;
                return (
                  <tr key={g.bu}>
                    <td className="mono">urn:kb:graph:{g.bu}</td>
                    <td>{g.entities}</td>
                    <td>{g.exact ?? 0}</td>
                    <td>{g.accepted ?? 0}</td>
                    <td>{g.pending ?? 0}</td>
                    <td>{g.unmapped ?? 0}</td>
                    <td>
                      <div className="row" style={{ gap: 8 }}>
                        <div className="bar" title={`${mapped} of ${g.entities}`}>
                          <i style={{ width: `${Math.round(share * 100)}%` }} />
                        </div>
                        {Math.round(share * 100)}%
                      </div>
                    </td>
                    <td>{g.chunks}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="muted" style={{ marginBottom: 0 }}>
            Explore the entities on the <Link to="/ontology">graph</Link>. Target is 80% mapped; the rest waits in the checker
            queue (D-043).
          </p>
        </div>
      )}
    </>
  );
}
