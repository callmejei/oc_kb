import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Head, Loading, when } from "../parts";

// Data product registration (Phase 9b, D-066). Mart access policies (grants, row filters, masks) are governed here:
// any change that could widen access is a revision a checker must approve; restricting access is single-actor and
// takes effect on the next query. Governing a product never grants reading it.
type Mask = { mask: string; unless_groups: string[] };
interface Spec {
  grants: string[];
  row_filters: { groups: string[]; filter: string }[];
  column_masks: Record<string, Mask>;
}
interface Product {
  product_id: string;
  status: string;
  revision_no: number | null;
  owner: string | null;
  description: string | null;
  classification: string | null;
  refresh: string | null;
  spec: Spec | null;
  pending: number;
}
interface List {
  products: Product[];
  unregistered: string[];
  policy_version: number;
  maker: boolean;
  checker: boolean;
  columns: Record<string, string[]>;
}
interface Diff {
  kind: string;
  item: string;
  change: string;
  widens: boolean;
}
interface Req {
  request_id: string;
  request_type: string;
  product_id: string;
  maker_id: string;
  maker_comment: string | null;
  submitted_at: string;
  status: string;
  revision_no: number | null;
  proposed: Spec | null;
  diff?: Diff[];
  widens?: boolean;
  stale: boolean;
  own: boolean;
}
interface DraftRow {
  revision_id: string;
  product_id: string;
  revision_no: number;
  spec: Spec;
  created_at: string;
}

const P = "/products";

// ---- spec <-> text: one line per item, so a maker can review exactly what is proposed
function specToText(s: Spec | null): {
  grants: string;
  filters: string;
  masks: string;
} {
  if (!s) return { grants: "", filters: "", masks: "" };
  return {
    grants: s.grants.join("\n"),
    filters: s.row_filters
      .map((f) => `${f.groups.join(", ")}: ${f.filter}`)
      .join("\n"),
    masks: Object.entries(s.column_masks)
      .map(
        ([c, m]) =>
          `${c}: ${m.mask}${m.unless_groups.length ? ` unless ${m.unless_groups.join(", ")}` : ""}`,
      )
      .join("\n"),
  };
}
function textToSpec(t: {
  grants: string;
  filters: string;
  masks: string;
}): Spec {
  const lines = (s: string) =>
    s
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
  const groups = (s: string) =>
    s
      .split(",")
      .map((g) => g.trim())
      .filter(Boolean);
  return {
    grants: lines(t.grants),
    row_filters: lines(t.filters).map((l) => {
      const i = l.indexOf(":");
      return { groups: groups(l.slice(0, i)), filter: l.slice(i + 1).trim() };
    }),
    column_masks: Object.fromEntries(
      lines(t.masks).map((l) => {
        const [col, rest] = [
          l.slice(0, l.indexOf(":")).trim(),
          l.slice(l.indexOf(":") + 1).trim(),
        ];
        const [mask, unless] = rest.split(/\s+unless\s+/);
        return [
          col,
          { mask: mask.trim(), unless_groups: unless ? groups(unless) : [] },
        ];
      }),
    ),
  };
}

function SpecView({ s }: { s: Spec }) {
  return (
    <table>
      <tbody>
        <tr>
          <th>Granted to</th>
          <td>
            {s.grants.length ? (
              s.grants.map((g) => (
                <span key={g} className="chip mono" style={{ marginRight: 4 }}>
                  {g}
                </span>
              ))
            ) : (
              <span className="chip bad">nobody</span>
            )}
          </td>
        </tr>
        <tr>
          <th>Row filters</th>
          <td>
            {s.row_filters.map((f, i) => (
              <div key={i}>
                <span className="mono">{f.groups.join(", ")}</span> →{" "}
                <span className="mono">{f.filter}</span>
                {f.filter.toUpperCase() === "TRUE" && (
                  <span className="chip warn" style={{ marginLeft: 6 }}>
                    all rows
                  </span>
                )}
              </div>
            ))}
            {s.grants
              .filter((g) => !s.row_filters.some((f) => f.groups.includes(g)))
              .map((g) => (
                <div key={g} className="muted">
                  <span className="mono">{g}</span> → no row filter: sees no
                  rows (deny by default)
                </div>
              ))}
          </td>
        </tr>
        <tr>
          <th>Masked columns</th>
          <td>
            {Object.keys(s.column_masks).length === 0 && (
              <span className="muted">none</span>
            )}
            {Object.entries(s.column_masks).map(([c, m]) => (
              <div key={c}>
                <span className="mono">{c}</span>: {m.mask}
                {m.unless_groups.length > 0 && (
                  <span className="muted">
                    {" "}
                    (except {m.unless_groups.join(", ")})
                  </span>
                )}
              </div>
            ))}
          </td>
        </tr>
      </tbody>
    </table>
  );
}

function Editor({
  list,
  start,
  onDone,
}: {
  list: List;
  start?: Product;
  onDone: () => void;
}) {
  const [table, setTable] = useState(
    start?.product_id ?? list.unregistered[0] ?? "",
  );
  const [meta, setMeta] = useState({
    owner: start?.owner ?? "",
    description: start?.description ?? "",
    classification: start?.classification ?? "INTERNAL",
    refresh: start?.refresh ?? "daily",
  });
  const [t, setT] = useState(specToText(start?.spec ?? null));
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    setErr(null);
    try {
      await api.post(`${P}/drafts`, {
        product_id: table,
        ...meta,
        spec: textToSpec(t),
      });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className="panel" data-testid="product-editor">
      <h2 style={{ marginTop: 0 }}>
        {start ? `Change ${start.product_id}` : "Register a data product"}
      </h2>
      {!start && (
        <label>
          Mart table{" "}
          <select
            value={table}
            onChange={(e) => setTable(e.target.value)}
            data-testid="pe-table"
          >
            {Object.keys(list.columns).map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
      )}
      <p className="muted mono" style={{ fontSize: 12 }}>
        columns: {(list.columns[table] ?? []).join(", ")}
      </p>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          placeholder="Owner"
          value={meta.owner}
          onChange={(e) => setMeta({ ...meta, owner: e.target.value })}
          data-testid="pe-owner"
        />
        <input
          placeholder="Refresh"
          value={meta.refresh}
          onChange={(e) => setMeta({ ...meta, refresh: e.target.value })}
        />
        <select
          value={meta.classification}
          onChange={(e) => setMeta({ ...meta, classification: e.target.value })}
        >
          {["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"].map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
      </div>
      <textarea
        placeholder="Description"
        value={meta.description}
        onChange={(e) => setMeta({ ...meta, description: e.target.value })}
        rows={2}
        style={{ width: "100%", marginTop: 8 }}
        data-testid="pe-description"
      />
      <h3>Granted groups (one per line)</h3>
      <textarea
        rows={3}
        style={{ width: "100%" }}
        className="mono"
        value={t.grants}
        onChange={(e) => setT({ ...t, grants: e.target.value })}
        data-testid="pe-grants"
      />
      <h3>Row filters (GROUP[, GROUP]: condition)</h3>
      <textarea
        rows={3}
        style={{ width: "100%" }}
        className="mono"
        value={t.filters}
        onChange={(e) => setT({ ...t, filters: e.target.value })}
        data-testid="pe-filters"
      />
      <p className="muted">
        Conditions use this table's columns, literals, comparisons, IN (…), IS
        NULL, BETWEEN, AND/OR/NOT. No functions or subqueries. A granted group
        with no filter sees no rows.
      </p>
      <h3>
        Masked columns (column: redact|hash|year_only|null [unless GROUP, …])
      </h3>
      <textarea
        rows={3}
        style={{ width: "100%" }}
        className="mono"
        value={t.masks}
        onChange={(e) => setT({ ...t, masks: e.target.value })}
        data-testid="pe-masks"
      />
      {err && (
        <div className="error" data-testid="pe-error">
          {err}
        </div>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button className="primary" onClick={save} data-testid="pe-save">
          Save draft
        </button>
        <button className="link" onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}

export default function Products() {
  const [list, setList] = useState<List | null>(null);
  const [reqs, setReqs] = useState<Req[]>([]);
  const [drafts, setDrafts] = useState<DraftRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [editing, setEditing] = useState<Product | "new" | null>(null);
  const load = useCallback(() => {
    Promise.all([
      api.get<List>(P),
      api.get<Req[]>(`${P}/requests`),
      api.get<DraftRow[]>(`${P}/drafts`),
    ])
      .then(([l, r, d]) => {
        setList(l);
        setReqs(r);
        setDrafts(d);
      })
      .catch((e: Error) => setErr(e.message));
  }, []);
  useEffect(load, [load]);
  const act = async (f: () => Promise<unknown>, ok: string) => {
    setErr(null);
    setMsg(null);
    try {
      await f();
      setMsg(ok);
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  if (!list) return <Loading err={err} />;
  const restrict = (p: Product) => {
    const action = window.prompt(
      "Restrict how? REVOKE_GRANT, ADD_MASK or SUSPEND",
      "REVOKE_GRANT",
    );
    if (!action) return;
    const body: Record<string, string> = {
      action: action.trim().toUpperCase(),
    };
    if (body.action === "REVOKE_GRANT")
      body.group =
        window.prompt("Group to revoke", p.spec?.grants[0] ?? "") ?? "";
    if (body.action === "ADD_MASK") {
      body.column = window.prompt("Column to mask", "") ?? "";
      body.mask =
        window.prompt("Mask (redact, hash, year_only, null)", "redact") ??
        "redact";
    }
    body.reason = window.prompt("Reason (audited)", "") ?? "";
    act(
      () => api.post(`${P}/${p.product_id}/restrict`, body),
      `${p.product_id}: restricted; applies to the next query.`,
    );
  };
  return (
    <>
      <Head eyebrow="Data Management" title="Data product registration">
        Each mart table is a governed product: its access policy (who may query
        it, which rows, which columns are masked) changes only through maker →
        checker. Restricting access is single-actor and immediate. Policy
        version <b data-testid="policy-version">{list.policy_version}</b>.
      </Head>
      {msg && (
        <div className="panel ok" data-testid="pp-msg">
          {msg}
        </div>
      )}
      {err && (
        <div className="error" data-testid="pp-error">
          {err}
        </div>
      )}
      {editing && (
        <Editor
          list={list}
          start={editing === "new" ? undefined : editing}
          onDone={() => {
            setEditing(null);
            load();
          }}
        />
      )}

      {list.checker && (
        <div className="panel" data-testid="pp-requests">
          <h2 style={{ marginTop: 0 }}>
            Waiting for a checker ({reqs.length})
          </h2>
          {reqs.length === 0 && <p className="muted">Nothing pending.</p>}
          {reqs.map((r) => (
            <div
              key={r.request_id}
              className="panel"
              data-testid={`pp-req-${r.product_id}`}
            >
              <div className="row">
                <b className="mono">{r.product_id}</b>
                <span className="chip">
                  {r.request_type.replace("_PRODUCT", "").toLowerCase()}
                </span>
                {r.widens && <span className="chip warn">widens access</span>}
                {r.stale && (
                  <span className="chip bad">
                    stale: product changed since drafted
                  </span>
                )}
                <span className="grow" />
                <span className="muted">
                  by {r.maker_id} · {when(r.submitted_at)}
                </span>
              </div>
              {r.maker_comment && <p>“{r.maker_comment}”</p>}
              <table>
                <tbody>
                  {(r.diff ?? []).map((d, i) => (
                    <tr key={i} className={d.widens ? "bad" : ""}>
                      <td>{d.kind.replace("_", " ")}</td>
                      <td className="mono">{d.item}</td>
                      <td className="mono">{d.change}</td>
                      <td>
                        {d.widens ? (
                          <span className="chip warn">widens</span>
                        ) : (
                          <span className="chip ok">narrows</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {r.proposed && (
                <details>
                  <summary>Full proposed policy</summary>
                  <SpecView s={r.proposed} />
                </details>
              )}
              {r.own ? (
                <p className="muted">
                  Your own request: another checker must decide (four eyes).
                </p>
              ) : (
                <div className="row" style={{ marginTop: 8 }}>
                  <button
                    className="primary"
                    onClick={() =>
                      act(
                        () =>
                          api.post(`${P}/requests/${r.request_id}/decide`, {
                            approve: true,
                            reason: "approved",
                          }),
                        "Approved; the policy applies to the next query.",
                      )
                    }
                    data-testid="pp-approve"
                  >
                    Approve
                  </button>
                  <button
                    onClick={() => {
                      const reason = window.prompt(
                        "Reason for rejecting (required)",
                      );
                      if (reason)
                        act(
                          () =>
                            api.post(`${P}/requests/${r.request_id}/decide`, {
                              approve: false,
                              reason,
                            }),
                          "Rejected.",
                        );
                    }}
                  >
                    Reject
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {list.maker && (
        <div className="panel" data-testid="pp-drafts">
          <div className="row">
            <h2 style={{ margin: 0 }}>My drafts</h2>
            <span className="grow" />
            {!editing && list.unregistered.length > 0 && (
              <button onClick={() => setEditing("new")} data-testid="pp-new">
                Register a product
              </button>
            )}
          </div>
          {drafts.length === 0 && <p className="muted">No drafts.</p>}
          {drafts.map((d) => (
            <div
              key={d.revision_id}
              className="row"
              data-testid={`pp-draft-${d.product_id}`}
            >
              <span className="mono grow">
                {d.product_id} · revision {d.revision_no}
              </span>
              <button
                className="primary"
                onClick={() =>
                  act(
                    () => api.post(`${P}/drafts/${d.revision_id}/submit`, {}),
                    "Submitted for a checker.",
                  )
                }
                data-testid="pp-submit"
              >
                Submit
              </button>
              <button
                className="link"
                onClick={() =>
                  act(
                    () => api.del(`${P}/drafts/${d.revision_id}`),
                    "Draft discarded.",
                  )
                }
              >
                Discard
              </button>
            </div>
          ))}
        </div>
      )}

      {list.products.map((p) => (
        <div
          key={p.product_id}
          className="panel"
          data-testid={`pp-product-${p.product_id}`}
        >
          <div className="row">
            <h2 className="mono" style={{ margin: 0 }}>
              {p.product_id}
            </h2>
            <span className={`chip ${p.status === "ACTIVE" ? "ok" : "bad"}`}>
              {p.status.toLowerCase()}
            </span>
            {p.classification && (
              <span className="chip">{p.classification.toLowerCase()}</span>
            )}
            {p.pending > 0 && <span className="chip warn">change pending</span>}
            <span className="grow" />
            <span className="muted">
              revision {p.revision_no ?? "—"} · owner {p.owner ?? "—"} ·{" "}
              {p.refresh ?? ""}
            </span>
          </div>
          {p.description && <p className="muted">{p.description}</p>}
          {p.spec && <SpecView s={p.spec} />}
          <div className="row" style={{ marginTop: 8 }}>
            {list.maker && p.status === "ACTIVE" && !editing && (
              <button onClick={() => setEditing(p)} data-testid="pp-change">
                Propose a change
              </button>
            )}
            {p.status === "ACTIVE" && (
              <button onClick={() => restrict(p)} data-testid="pp-restrict">
                Restrict now
              </button>
            )}
            {list.maker && p.status === "SUSPENDED" && (
              <button
                onClick={() =>
                  act(
                    () =>
                      api.post(`${P}/${p.product_id}/reactivate`, {
                        comment: "reactivate",
                      }),
                    "Reactivation requested; a checker must approve.",
                  )
                }
              >
                Request reactivation
              </button>
            )}
          </div>
        </div>
      ))}
      {list.unregistered.length > 0 && (
        <p className="muted">
          Not registered, so denied to everyone:{" "}
          <span className="mono">{list.unregistered.join(", ")}</span>
        </p>
      )}
    </>
  );
}
