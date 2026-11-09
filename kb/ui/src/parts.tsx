import { useEffect, useState, type ReactNode } from "react";
import { api } from "./api";

export function Head({ eyebrow, title, children }: { eyebrow: string; title: string; children?: ReactNode }) {
  return (
    <div className="vh">
      <div className="eyebrow">{eyebrow}</div>
      <h1>{title}</h1>
      {children && <p>{children}</p>}
    </div>
  );
}

/** GET a gateway path once; returns [data, error]. */
export function useGet<T>(path: string): [T | null, string | null] {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setData(null);
    setErr(null);
    api
      .get<T>(path)
      .then((d) => live && setData(d))
      .catch((e: Error) => live && setErr(e.message));
    return () => {
      live = false;
    };
  }, [path]);
  return [data, err];
}

export function Loading({ err }: { err: string | null }) {
  return err ? <div className="error">{err}</div> : <p className="muted">Loading…</p>;
}

export function when(ts: string | null | undefined): string {
  return ts ? new Date(ts).toLocaleString() : "—";
}
