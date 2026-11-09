import { Link } from "react-router-dom";
import { useSession } from "../session";
import { Head, Loading, useGet } from "../parts";

interface Entry {
  concept: string;
  curie: string;
  label: string;
  table: string;
  hint: string;
}

export default function Mapping() {
  const { me } = useSession();
  const [data, err] = useGet<{ entries: Entry[]; hidden: number; source: string }>("/explorer/catalog/table-map");
  const byTable = new Map<string, Entry[]>();
  for (const e of data?.entries ?? []) byTable.set(e.table, [...(byTable.get(e.table) ?? []), e]);
  return (
    <>
      <Head eyebrow="Ontology & Semantic Management" title="Entity & relationship mapping">
        Which ontology concepts map to which mart columns. Text-to-SQL gets these as hints, but only for concepts in the question
        and only for tables you are granted.
      </Head>
      <div className="notice" data-testid="mapping-readonly">
        Read-only. The mapping lives in <span className="mono">{data?.source ?? "config/table_map.yaml"}</span>; changing it is a
        reviewed code change. Entity-to-concept mappings below the auto-accept threshold go to the checkers'{" "}
        {me && me.checker_bus.length > 0 ? <Link to="/inbox">concept review queue</Link> : "concept review queue"}.
      </div>
      {!data ? (
        <Loading err={err} />
      ) : (
        <>
          {[...byTable.entries()].map(([table, rows]) => (
            <div className="panel" key={table}>
              <h2 className="mono" style={{ marginTop: 0 }}>
                {table}
              </h2>
              <table>
                <thead>
                  <tr>
                    <th>Concept</th>
                    <th>Identifier</th>
                    <th>Mart columns / condition</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.concept}>
                      <td>{r.label}</td>
                      <td className="mono">{r.curie}</td>
                      <td className="mono">{r.hint}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
          {data.entries.length === 0 && <div className="panel muted">No mappings for tables you are granted.</div>}
          {data.hidden > 0 && <p className="muted">{data.hidden} mappings point at tables you are not granted.</p>}
        </>
      )}
    </>
  );
}
