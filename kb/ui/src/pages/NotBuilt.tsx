import { Navigate, useParams } from "react-router-dom";
import { findNotBuilt } from "../nav";
import { Head } from "../parts";

export default function NotBuilt() {
  const { slug = "" } = useParams();
  const hit = findNotBuilt(slug);
  if (!hit) return <Navigate to="/chat" replace />;
  return (
    <>
      <Head eyebrow={hit.group} title={hit.item.label} />
      <div className="panel" data-testid="not-built">
        <p>
          <span className="chip">Not built</span> This screen is part of the explorer concept, but nothing behind it exists in
          the prototype yet, so it shows no data rather than sample numbers.
        </p>
        <p>
          <b>What it would take:</b> {hit.item.notBuilt}.
        </p>
        <p className="muted" style={{ marginBottom: 0 }}>
          Any such feature would keep the prototype's rules: one door (the gateway), access enforced before retrieval, four eyes
          in the database, and local models only.
        </p>
      </div>
    </>
  );
}
