import { Head, Loading, useGet } from "../parts";

interface MyAccess {
  user_id: string;
  groups: string[];
  readable_bus: string[];
  maker_bus: string[];
  checker_bus: string[];
  tables: { table: string; row_filter: string; masked: Record<string, string> }[];
}

export default function Access() {
  const [a, err] = useGet<MyAccess>("/explorer/governance/my-access");
  return (
    <>
      <Head eyebrow="Data/Knowledge Governance" title="Access policies & classification">
        What your directory groups give you, resolved live from the identity provider: document spaces, governance roles, and
        how the data policies apply to you. Changing groups is done in the directory, not here.
      </Head>
      {!a ? (
        <Loading err={err} />
      ) : (
        <>
          <div className="tiles">
            <div className="tile">
              <div className="tile-label">Readable BU spaces</div>
              <div className="tile-value">{a.readable_bus.length}</div>
              <div className="tile-note">{a.readable_bus.join(", ") || "none"}</div>
            </div>
            <div className="tile">
              <div className="tile-label">Maker in</div>
              <div className="tile-value">{a.maker_bus.length}</div>
              <div className="tile-note">{a.maker_bus.join(", ") || "—"}</div>
            </div>
            <div className="tile">
              <div className="tile-label">Checker in</div>
              <div className="tile-value">{a.checker_bus.length}</div>
              <div className="tile-note">{a.checker_bus.join(", ") || "—"}</div>
            </div>
            <div className="tile">
              <div className="tile-label">Mart tables granted</div>
              <div className="tile-value">{a.tables.length}</div>
              <div className="tile-note">row filters and masks below</div>
            </div>
          </div>
          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Your groups</h2>
            <div data-testid="my-groups">
              {a.groups.length ? a.groups.map((g) => <span className="chip mono" key={g}>{g}</span>) : <span className="muted">No groups: no content.</span>}
            </div>
          </div>
          {a.tables.length > 0 && (
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Data policies applied to you</h2>
              <table>
                <thead>
                  <tr>
                    <th>Table</th>
                    <th>Row filter</th>
                    <th>Masked columns</th>
                  </tr>
                </thead>
                <tbody>
                  {a.tables.map((t) => (
                    <tr key={t.table}>
                      <td className="mono">{t.table}</td>
                      <td className="mono">{t.row_filter}</td>
                      <td>
                        {Object.keys(t.masked).length
                          ? Object.entries(t.masked).map(([c, m]) => (
                              <span className="chip warn" key={c}>
                                {c} ({m})
                              </span>
                            ))
                          : "none"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  );
}
