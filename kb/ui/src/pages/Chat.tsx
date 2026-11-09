import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  api,
  label,
  PROBLEMS,
  type AskResponse,
  type Citation,
  type Problem,
} from "../api";
import { useSession } from "../session";

interface Turn {
  question: string;
  spaces: string[];
  res?: AskResponse;
  error?: string;
}

export default function Chat() {
  const { me } = useSession();
  const [spaces, setSpaces] = useState<string[]>([]); // empty = every space the user can read
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);

  async function ask(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (q.length < 2) return;
    setBusy(true);
    setQuestion("");
    const turn: Turn = { question: q, spaces };
    setTurns((t) => [...t, turn]);
    try {
      turn.res = await api.post<AskResponse>("/ask", {
        question: q,
        spaces: spaces.length ? spaces : null,
      });
    } catch (err) {
      turn.error = err instanceof Error ? err.message : String(err);
    }
    setTurns((t) => [...t.slice(0, -1), { ...turn }]);
    setBusy(false);
  }

  function toggle(bu: string) {
    setSpaces((s) => (s.includes(bu) ? s.filter((x) => x !== bu) : [...s, bu]));
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <h1 className="grow">Ask</h1>
        <span className="muted">Spaces:</span>
        {me!.spaces.length === 0 && (
          <span className="muted">none (you have no document access)</span>
        )}
        {me!.spaces.map((bu) => (
          <label key={bu} className="row" style={{ gap: 4 }}>
            <input
              type="checkbox"
              checked={spaces.includes(bu)}
              onChange={() => toggle(bu)}
              data-testid={`space-${bu}`}
            />
            {bu}
          </label>
        ))}
        <span className="muted">{spaces.length ? "" : "(all)"}</span>
      </div>
      <div className="chat-log">
        {turns.map((t, i) => (
          <div key={i} className="chat-log">
            <div className="msg user">{t.question}</div>
            {t.error && <div className="error">{t.error}</div>}
            {!t.res && !t.error && <div className="msg muted">Thinking…</div>}
            {t.res && <Answer res={t.res} />}
          </div>
        ))}
      </div>
      <form className="ask" onSubmit={ask}>
        <textarea
          name="question"
          placeholder="Ask a question about your documents or data"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) ask(e);
          }}
        />
        <button
          className="primary"
          type="submit"
          disabled={busy || question.trim().length < 2}
        >
          Ask
        </button>
      </form>
    </div>
  );
}

function Answer({ res }: { res: AskResponse }) {
  return (
    <div className="msg" data-testid="answer">
      <div className="answer">{res.answer}</div>
      {res.notes.map((n) => (
        <div key={n} className="muted">
          {n}
        </div>
      ))}
      {res.citations.length > 0 && (
        <div className="cites">
          {res.citations.map((c) => (
            <CitationLine key={String(c.n)} c={c} />
          ))}
        </div>
      )}
      <div className="row muted" style={{ marginTop: 8, fontSize: 12 }}>
        <span>route: {res.route}</span>
        <span>{(res.latency_ms / 1000).toFixed(1)} s</span>
        <span className="grow" />
        <Feedback queryId={res.query_id} />
      </div>
    </div>
  );
}

function CitationLine({ c }: { c: Citation }) {
  if (c.kind === "data")
    return (
      <div data-testid="citation-data">
        [{c.n}] tables {c.tables?.join(", ")} ({c.row_count} rows)
        {c.metric && (
          <span
            className="chip ok"
            data-testid="citation-metric"
            style={{ marginLeft: 6 }}
          >
            governed metric: {c.metric.label} (revision {c.metric.revision})
          </span>
        )}
        {c.sql && <pre>{c.sql}</pre>}
      </div>
    );
  const page = c.page
    ? c.page_end && c.page_end !== c.page
      ? `pp. ${c.page}-${c.page_end}`
      : `p. ${c.page}`
    : "";
  return (
    <div data-testid="citation-doc">
      [{c.n}]{" "}
      <Link
        to={`/documents/${c.doc_id}?v=${c.version_no ?? ""}&page=${c.page ?? ""}&chunk=${c.chunk_id ?? ""}`}
      >
        {c.title ?? c.doc_id}
      </Link>{" "}
      <span className="muted">
        {c.section ? `§ ${c.section} · ` : ""}
        {page} · v{c.version_no}
      </span>
    </div>
  );
}

function Feedback({ queryId }: { queryId: string }) {
  const [state, setState] = useState<"idle" | "down" | "sent">("idle");
  const [problem, setProblem] = useState<Problem>("WRONG_ANSWER");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function send(rating: "UP" | "DOWN") {
    setError(null);
    try {
      await api.post("/feedback", {
        query_id: queryId,
        rating,
        problem: rating === "DOWN" ? problem : null,
        reason: rating === "DOWN" ? reason : null,
      });
      setState("sent");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  if (state === "sent")
    return <span data-testid="feedback-sent">Thanks, feedback recorded.</span>;
  if (state === "idle")
    return (
      <span className="row" style={{ gap: 6 }}>
        <button
          onClick={() => send("UP")}
          title="Helpful"
          aria-label="thumbs up"
        >
          👍
        </button>
        <button
          onClick={() => setState("down")}
          title="Not helpful"
          aria-label="thumbs down"
          data-testid="thumbs-down"
        >
          👎
        </button>
      </span>
    );
  return (
    <span className="row" style={{ gap: 6 }}>
      <select
        value={problem}
        onChange={(e) => setProblem(e.target.value as Problem)}
        data-testid="problem"
      >
        {PROBLEMS.map((p) => (
          <option key={p} value={p}>
            {label(p)}
          </option>
        ))}
      </select>
      <input
        placeholder="What was wrong?"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        data-testid="reason"
        style={{ minWidth: 260 }}
      />
      <button
        className="primary"
        disabled={!reason.trim()}
        onClick={() => send("DOWN")}
        data-testid="send-feedback"
      >
        Send
      </button>
      {error && <span className="error">{error}</span>}
    </span>
  );
}
