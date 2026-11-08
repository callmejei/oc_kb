import { useEffect, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

export interface Region {
  ref: string;
  label: string;
  bbox: [number, number, number, number]; // l, t, r, b in PDF points, top-left origin
}

interface Props {
  data: ArrayBuffer;
  page: number;
  regions: Region[];
  active: string | null;
  onPick: (ref: string) => void;
  onPages?: (n: number) => void;
  width?: number;
}

/** One PDF page on a canvas, with Docling region boxes drawn on top (same coordinates, scaled). */
export default function PdfPage({ data, page, regions, active, onPick, onPages, width = 640 }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [doc, setDoc] = useState<pdfjs.PDFDocumentProxy | null>(null);
  const [scale, setScale] = useState(1);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // a copy: pdf.js transfers the buffer to its worker
    const task = pdfjs.getDocument({ data: new Uint8Array(data.slice(0)), isEvalSupported: false, enableXfa: false });
    task.promise
      .then((d) => {
        if (cancelled) return;
        setDoc(d);
        onPages?.(d.numPages);
      })
      .catch((e) => setError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
      task.destroy();
    };
  }, [data, onPages]);

  useEffect(() => {
    if (!doc || !canvas.current) return;
    let task: pdfjs.RenderTask | null = null;
    let cancelled = false;
    doc.getPage(Math.min(Math.max(page, 1), doc.numPages)).then((p) => {
      if (cancelled || !canvas.current) return;
      const base = p.getViewport({ scale: 1 });
      const s = width / base.width;
      const vp = p.getViewport({ scale: s });
      const c = canvas.current;
      c.width = Math.floor(vp.width);
      c.height = Math.floor(vp.height);
      setScale(s);
      setSize({ w: vp.width, h: vp.height });
      task = p.render({ canvasContext: c.getContext("2d")!, viewport: vp });
      task.promise.catch(() => undefined);
    });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, page, width]);

  if (error) return <div className="error">Cannot render PDF: {error}</div>;
  return (
    <div className="pagebox" style={size ? { width: size.w, height: size.h } : undefined} data-testid="pdf-page">
      <canvas ref={canvas} />
      {regions.map((r) => {
        const [l, t, rr, b] = r.bbox;
        return (
          <div
            key={`${r.ref}-${l}-${t}`}
            className={`region ${r.label === "table" ? "table" : ""} ${active === r.ref ? "active" : ""}`}
            title={`${r.label} ${r.ref}`}
            data-testid="region"
            style={{ left: l * scale, top: t * scale, width: (rr - l) * scale, height: (b - t) * scale }}
            onClick={() => onPick(r.ref)}
          />
        );
      })}
    </div>
  );
}
