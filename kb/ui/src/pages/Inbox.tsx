import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { RequestDetail } from "./RequestDetail";

interface InboxItem {
  request_id: string;
  request_type: string;
  source_id: string;
  source_name: string;
  bu: string;
  maker_id: string;
  maker_comment: string | null;
  submitted_at: string;
  doc_title: string | null;
}

interface ConceptReview {
  review_id: string;
  bu: string;
  entity_label: string;
  proposed_label: string;
  proposed_concept: string;
  confidence: string;
  method: string;
  context: string | null;
}

export default function Inbox() {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [reviews, setReviews] = useState<ConceptReview[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tab, setTab] = useState<"requests" | "concepts">("requests");

  const refresh = useCallback(async () => {
    try {
      setItems(await api.gov.get<InboxItem[]>("/inbox"));
      setReviews(await api.gov.get<ConceptReview[]>("/concept-reviews"));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err)); // e.g. 403: not a CHECKER
    }
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [refresh]);

  async function decide(id: string, approve: boolean) {
    setError(null);
    setNotice(null);
    try {
      await api.gov.post(`/requests/${id}/${approve ? "approve" : "reject"}`, { reason: reason || null });
      setNotice(`Request ${id.slice(0, 8)} ${approve ? "approved" : "rejected"}.`);
      setOpen(null);
      setReason("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function concept(id: string, accept: boolean) {
    setError(null);
    try {
      const r = accept ? null : prompt("Why reject this mapping?");
      if (!accept && !r) return;
      await api.gov.post(`/concept-reviews/${id}/${accept ? "accept" : "reject"}`, { reason: r });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div>
      <h1>Checker inbox</h1>
      <div className="tabs">
        <button className={tab === "requests" ? "active" : ""} onClick={() => setTab("requests")}>
          Requests ({items.length})
        </button>
        <button className={tab === "concepts" ? "active" : ""} onClick={() => setTab("concepts")}>
          Concept mappings ({reviews.length})
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}
      {tab === "requests" && (
        <div className="cols">
          <div className="panel">
            {items.length === 0 && <p className="muted">Nothing waiting for you.</p>}
            <table data-testid="inbox">
              <tbody>
                {items.map((i) => (
                  <tr key={i.request_id} data-testid={`req-${i.request_id}`} className={open === i.request_id ? "active" : ""}>
                    <td>
                      <span className="chip">{i.request_type}</span> <b>{i.source_id}</b>
                      {i.doc_title && <div>{i.doc_title}</div>}
                      <div className="muted">
                        {i.bu} · by {i.maker_id} · {new Date(i.submitted_at).toLocaleString()}
                      </div>
                      {i.maker_comment && <div>“{i.maker_comment}”</div>}
                    </td>
                    <td>
                      <button onClick={() => setOpen(i.request_id)}>Review</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="panel">
            {open ? (
              <>
                <RequestDetail requestId={open} />
                <label className="field" style={{ marginTop: 12 }}>
                  Reason (required to reject)
                  <textarea value={reason} onChange={(e) => setReason(e.target.value)} data-testid="decision-reason" />
                </label>
                <div className="row" style={{ marginTop: 8 }}>
                  <button className="primary" onClick={() => decide(open, true)} data-testid="approve">
                    Approve
                  </button>
                  <button className="danger" disabled={!reason.trim()} onClick={() => decide(open, false)} data-testid="reject">
                    Reject
                  </button>
                </div>
              </>
            ) : (
              <p className="muted">Pick a request to review.</p>
            )}
          </div>
        </div>
      )}
      {tab === "concepts" && (
        <div className="panel">
          <p className="muted">
            Proposed FIBO mappings below the auto-accept threshold. Nothing here is used for graph expansion until you
            accept it.
          </p>
          <table>
            <thead>
              <tr>
                <th>BU</th>
                <th>Entity</th>
                <th>Proposed concept</th>
                <th>Confidence</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {reviews.slice(0, 200).map((r) => (
                <tr key={r.review_id}>
                  <td>{r.bu}</td>
                  <td>{r.entity_label}</td>
                  <td title={r.proposed_concept}>{r.proposed_label}</td>
                  <td>
                    {Number(r.confidence).toFixed(2)} <span className="muted">{r.method}</span>
                  </td>
                  <td className="row" style={{ gap: 6 }}>
                    <button onClick={() => concept(r.review_id, true)}>Accept</button>
                    <button className="danger" onClick={() => concept(r.review_id, false)}>
                      Reject
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
