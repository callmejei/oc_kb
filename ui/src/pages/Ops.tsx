import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";

interface SourceHealth {
  source_id: string;
  bu: string;
  display_name: string;
  source_type: string;
  status: string;
  last_run_kind: string | null;
  last_run_status: string | null;
  last_run_finished_at: string | null;
  last_success_at: string | null;
  hours_since_success: number | null;
  failed_runs_7d: number;
  indexed_docs: number;
  open_exceptions: number;
}
interface ExceptionRow {
  bu: string;
  reason: string;
  open: number;
  under_1d: number;
  d1_to_7: number;
  over_7d: number;
  oldest: string;
}
interface EvalResult {
  bu: string;
  suite: string;
  metric: string;
  value: number;
  n: number;
  threshold: number | null;
  passed: boolean | null;
}
interface EvalRun {
  run_id: string;
  started_at: string;
  finished_at: string;
  git_commit: string;
  status: "PASS" | "FAIL";
  llm: string;
  judge: string;
  report_uri: string | null;
  summary: { failures?: string[] };
  results: EvalResult[];
}
interface EvalTrend {
  runs: EvalRun[];
  leak: { run_id: string; at: string; leaks: number; tests: number; passed: boolean | null } | null;
}
interface FeedbackRow {
  bu: string;
  open: number;
  resolved: number;
  last_7d: number;
}

const STALE_HOURS = 48; // a SharePoint source should sync at least daily; two missed days is stale

// Categorical slots 1-3 of the validated palette (light surface): blue, orange, aqua.
const SERIES = [
  { suite: "golden", metric: "answer_correct", label: "Answer correct", color: "#2a78d6" },
  { suite: "ragas", metric: "faithfulness", label: "Faithfulness", color: "#eb6834" },
  { suite: "ragas", metric: "context_recall", label: "Context recall", color: "#1baf7a" },
] as const;

function when(ts: string | null): string {
  return ts ? new Date(ts).toLocaleString() : "—";
}

function overall(run: EvalRun, suite: string, metric: string): EvalResult | undefined {
  return run.results.find((r) => r.bu === "all" && r.suite === suite && r.metric === metric);
}

function Status({ ok, okText, badText }: { ok: boolean; okText: string; badText: string }) {
  return <span className={`chip ${ok ? "ok" : "bad"}`}>{ok ? `✓ ${okText}` : `✕ ${badText}`}</span>;
}

function Tile({ label, value, note, testid }: { label: string; value: React.ReactNode; note?: React.ReactNode; testid: string }) {
  return (
    <div className="tile" data-testid={testid}>
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}

/** Answer-quality trend: one line per metric over eval runs (oldest left), 0-1 scale, hover crosshair. */
function TrendChart({ runs }: { runs: EvalRun[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const data = useMemo(() => [...runs].reverse(), [runs]);
  if (data.length === 0) return <p className="muted">No evaluation runs recorded yet. Run `kb.ps1 eval`.</p>;
  const W = 720, H = 220, L = 40, R = 120, T = 12, B = 28; // prettier-ignore
  const x = (i: number) => (data.length === 1 ? L + (W - L - R) / 2 : L + (i * (W - L - R)) / (data.length - 1));
  const y = (v: number) => T + (1 - v) * (H - T - B);
  const pts = SERIES.map((s) => data.map((r, i) => ({ i, v: overall(r, s.suite, s.metric)?.value })));
  // end labels: placed at their line's last value, then pushed apart so no two overlap (>= 13 px)
  const ends = SERIES.map((_s, k) => {
    const valid = pts[k].filter((p) => p.v !== undefined) as { i: number; v: number }[];
    const last = valid[valid.length - 1];
    return last ? { k, x: x(last.i), y: y(last.v), v: last.v } : null;
  }).filter((e): e is { k: number; x: number; y: number; v: number } => e !== null);
  ends.sort((a, b) => a.y - b.y);
  ends.forEach((e, j) => {
    if (j > 0 && e.y - ends[j - 1].y < 13) e.y = ends[j - 1].y + 13;
  });
  const hovered = hover === null ? null : data[hover];
  return (
    <div className="trend">
      <div className="legend" aria-hidden="true">
        {SERIES.map((s) => (
          <span key={s.metric}>
            <i style={{ background: s.color }} /> {s.label}
          </span>
        ))}
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="Answer quality per evaluation run: answer correct, faithfulness, context recall (0 to 1)"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - box.left) / box.width) * W;
          let best = 0;
          data.forEach((_, i) => {
            if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i;
          });
          setHover(best);
        }}
      >
        {[0, 0.25, 0.5, 0.75, 1].map((g) => (
          <g key={g}>
            <line x1={L} x2={W - R} y1={y(g)} y2={y(g)} className="grid" />
            <text x={L - 6} y={y(g) + 4} className="axis" textAnchor="end">
              {g.toFixed(2)}
            </text>
          </g>
        ))}
        {data.map((r, i) =>
          data.length <= 12 || i % Math.ceil(data.length / 12) === 0 ? (
            <text key={r.run_id} x={x(i)} y={H - 8} className="axis" textAnchor="middle">
              {new Date(r.started_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
            </text>
          ) : null,
        )}
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} className="crosshair" />}
        {SERIES.map((s, k) => {
          const valid = pts[k].filter((p) => p.v !== undefined) as { i: number; v: number }[];
          const end = ends.find((e) => e.k === k);
          return (
            <g key={s.metric}>
              <polyline
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
                points={valid.map((p) => `${x(p.i)},${y(p.v)}`).join(" ")}
              />
              {valid.map((p) => (
                <circle key={p.i} cx={x(p.i)} cy={y(p.v)} r={hover === p.i ? 5 : 4} fill={s.color} stroke="#fff" strokeWidth={2} />
              ))}
              {end && (
                <text x={end.x + 10} y={end.y + 4} className="endlabel">
                  {s.label} {end.v.toFixed(2)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {hovered && (
        <div className="tip" data-testid="trend-tip">
          <b>{when(hovered.started_at)}</b> · {hovered.git_commit} · {hovered.status}
          {SERIES.map((s) => (
            <div key={s.metric}>
              <i style={{ background: s.color }} /> {s.label}: {overall(hovered, s.suite, s.metric)?.value.toFixed(3) ?? "—"}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Ops() {
  const [sources, setSources] = useState<SourceHealth[]>([]);
  const [exc, setExc] = useState<{ total_open: number; by_bu_reason: ExceptionRow[] }>({ total_open: 0, by_bu_reason: [] });
  const [trend, setTrend] = useState<EvalTrend>({ runs: [], leak: null });
  const [flags, setFlags] = useState<FeedbackRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, e, t, f] = await Promise.all([
        api.get<SourceHealth[]>("/ops/ingestion"),
        api.get<{ total_open: number; by_bu_reason: ExceptionRow[] }>("/ops/exceptions"),
        api.get<EvalTrend>("/ops/eval"),
        api.get<FeedbackRow[]>("/ops/feedback"),
      ]);
      setSources(s);
      setExc(e);
      setTrend(t);
      setFlags(f);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  const unhealthy = sources.filter(
    (s) =>
      s.status === "ACTIVE" &&
      (s.last_run_status === "FAILED" ||
        (s.source_type === "SHAREPOINT" && (s.hours_since_success === null || s.hours_since_success > STALE_HOURS))),
  );
  const latest = trend.runs[0];
  const latestAnswer = latest ? overall(latest, "golden", "answer_correct") : undefined;

  return (
    <div>
      <h1>Operations</h1>
      {error && <div className="error">{error}</div>}

      <div className="tiles">
        <Tile
          testid="tile-leak"
          label="Leak suite"
          value={trend.leak ? <Status ok={trend.leak.leaks === 0} okText="0 leaks" badText={`${trend.leak.leaks} leaks`} /> : "not run"}
          note={trend.leak ? `${trend.leak.tests} checks · ${when(trend.leak.at)}` : "run kb.ps1 eval"}
        />
        <Tile
          testid="tile-eval"
          label="Latest evaluation"
          value={latest ? <Status ok={latest.status === "PASS"} okText="PASS" badText="FAIL" /> : "none"}
          note={latestAnswer ? `answer correct ${latestAnswer.value.toFixed(2)} · ${latest.git_commit}` : undefined}
        />
        <Tile
          testid="tile-exceptions"
          label="Open exceptions"
          value={exc.total_open}
          note={`${exc.by_bu_reason.reduce((a, r) => a + r.over_7d, 0)} older than 7 days`}
        />
        <Tile
          testid="tile-sources"
          label="Sources needing attention"
          value={unhealthy.length}
          note={`of ${sources.filter((s) => s.status === "ACTIVE").length} active · stale after ${STALE_HOURS} h`}
        />
      </div>

      <div className="panel">
        <h2>Answer quality trend</h2>
        <TrendChart runs={trend.runs} />
        {latest?.summary.failures && latest.summary.failures.length > 0 && (
          <div className="muted">Latest run threshold failures: {latest.summary.failures.join("; ")}</div>
        )}
      </div>

      <div className="panel">
        <h2>Ingestion health</h2>
        <table data-testid="ops-sources">
          <thead>
            <tr>
              <th>Source</th>
              <th>BU</th>
              <th>Type</th>
              <th>Status</th>
              <th>Last run</th>
              <th>Last success</th>
              <th>Indexed docs</th>
              <th>Failed runs (7d)</th>
              <th>Open exceptions</th>
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.source_id}>
                <td>
                  {s.display_name} <div className="muted">{s.source_id}</div>
                </td>
                <td>{s.bu}</td>
                <td>{s.source_type}</td>
                <td>{s.status}</td>
                <td>
                  {s.last_run_kind ?? "—"}{" "}
                  {s.last_run_status && (
                    <span className={`chip ${s.last_run_status === "SUCCEEDED" ? "ok" : s.last_run_status === "FAILED" ? "bad" : "warn"}`}>
                      {s.last_run_status}
                    </span>
                  )}
                  <div className="muted">{when(s.last_run_finished_at)}</div>
                </td>
                <td>
                  {when(s.last_success_at)}
                  {s.hours_since_success !== null && <div className="muted">{s.hours_since_success} h ago</div>}
                </td>
                <td>{s.indexed_docs}</td>
                <td>{s.failed_runs_7d}</td>
                <td>{s.open_exceptions}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="cols">
        <div className="panel">
          <h2>Exception backlog</h2>
          {exc.by_bu_reason.length === 0 ? (
            <p className="muted">No open exceptions.</p>
          ) : (
            <table data-testid="ops-exceptions">
              <thead>
                <tr>
                  <th>BU</th>
                  <th>Reason</th>
                  <th>Open</th>
                  <th>&lt; 1 day</th>
                  <th>1–7 days</th>
                  <th>&gt; 7 days</th>
                  <th>Oldest</th>
                </tr>
              </thead>
              <tbody>
                {exc.by_bu_reason.map((r) => (
                  <tr key={`${r.bu}-${r.reason}`}>
                    <td>{r.bu}</td>
                    <td>{r.reason}</td>
                    <td>{r.open}</td>
                    <td>{r.under_1d}</td>
                    <td>{r.d1_to_7}</td>
                    <td>{r.over_7d}</td>
                    <td>{when(r.oldest)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="panel">
          <h2>Answer flags</h2>
          {flags.length === 0 ? (
            <p className="muted">No flags yet.</p>
          ) : (
            <table data-testid="ops-flags">
              <thead>
                <tr>
                  <th>BU</th>
                  <th>Open</th>
                  <th>Resolved</th>
                  <th>Last 7 days</th>
                </tr>
              </thead>
              <tbody>
                {flags.map((f) => (
                  <tr key={f.bu}>
                    <td>{f.bu}</td>
                    <td>{f.open}</td>
                    <td>{f.resolved}</td>
                    <td>{f.last_7d}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="panel">
        <h2>Evaluation runs</h2>
        <table data-testid="ops-runs">
          <thead>
            <tr>
              <th>Started</th>
              <th>Commit</th>
              <th>Status</th>
              {SERIES.map((s) => (
                <th key={s.metric}>{s.label}</th>
              ))}
              <th>Leaks</th>
              <th>Models</th>
              <th>Report</th>
            </tr>
          </thead>
          <tbody>
            {trend.runs.map((r) => (
              <tr key={r.run_id}>
                <td>{when(r.started_at)}</td>
                <td>{r.git_commit}</td>
                <td>
                  <Status ok={r.status === "PASS"} okText="PASS" badText="FAIL" />
                </td>
                {SERIES.map((s) => (
                  <td key={s.metric}>{overall(r, s.suite, s.metric)?.value.toFixed(3) ?? "—"}</td>
                ))}
                <td>{overall(r, "leak", "leaks")?.value ?? "—"}</td>
                <td className="muted">
                  {r.llm} · judge {r.judge}
                </td>
                <td className="muted">{r.report_uri ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
