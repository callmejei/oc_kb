import { Head, Loading, useGet } from "../parts";

interface Mart {
  table: string;
  description: string;
  rows: number;
  row_filter: string;
  columns: { name: string; description: string; mask: string | null }[];
}

export default function Marts() {
  const [data, err] = useGet<{ tables: Mart[]; not_granted: number }>("/explorer/catalog/marts");
  return (
    <>
      <Head eyebrow="Enterprise Marketplace" title="Data products">
        The mart tables you may query, as the policy engine applies them to you: your row filter and your column masks. Chat
        and text-to-SQL see exactly this.
      </Head>
      {!data ? (
        <Loading err={err} />
      ) : (
        <>
          {data.tables.length === 0 && <div className="panel muted">You have no table grants.</div>}
          {data.tables.map((t) => (
            <div className="panel" key={t.table} data-testid={`mart-${t.table}`}>
              <div className="row">
                <h2 className="mono" style={{ margin: 0 }}>
                  {t.table}
                </h2>
                <span className="muted">{t.rows.toLocaleString()} rows in the mart</span>
              </div>
              <p className="muted">{t.description}</p>
              <p>
                Your row filter: <span className="mono">{t.row_filter}</span>
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Column</th>
                    <th>Description</th>
                    <th>For you</th>
                  </tr>
                </thead>
                <tbody>
                  {t.columns.map((c) => (
                    <tr key={c.name}>
                      <td className="mono">{c.name}</td>
                      <td>{c.description}</td>
                      <td>{c.mask ? <span className="chip warn">masked ({c.mask})</span> : <span className="chip ok">visible</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
          {data.not_granted > 0 && (
            <p className="muted">
              {data.not_granted} other table{data.not_granted > 1 ? "s" : ""} exist that you are not granted.
            </p>
          )}
        </>
      )}
    </>
  );
}
