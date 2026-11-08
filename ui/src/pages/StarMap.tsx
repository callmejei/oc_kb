import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { api } from "../api";

interface FNode {
  k: "M" | "O" | "C" | "P" | "I" | "K";
  n: string;
  l: string;
  m: string;
  iri?: string;
  d?: string;
}
interface Fibo {
  release: string;
  modules: { id: string; label: string }[];
  counts: Record<string, number>;
  nodes: FNode[];
  edges: [number, number, number][];
  pos: [number, number, number][];
}
interface Entity {
  iri: string;
  label: string;
  bu: string;
  concept: string | null;
  match: "exact" | "accepted" | "pending" | "unmapped";
  confidence: number | null;
  chunks: number;
}
interface Overlay {
  bus: string[];
  entities: Entity[];
  counts: Record<string, number>;
}
interface EntityDetail {
  label: string;
  bu: string;
  concept: string | null;
  sources: { doc_id: string; title: string; version_no: number }[];
  mart_hints: string[];
}

const KIND: Record<FNode["k"], string> = {
  M: "Module",
  O: "Ontology",
  C: "Class",
  P: "Property",
  I: "Individual",
  K: "KB local concept",
};
const EDGE = ["contains", "subclass of", "domain", "range", "subproperty of", "type", "broader"];
// Colour by job: FIBO is neutral context; slot 1 = KB local concepts; slot 2 = the module picked in the filter;
// overlay entities wear status colours (good = mapped, warning = pending review), always with a text label.
const C_FIBO = new THREE.Color("#b4bac6");
const C_KB = new THREE.Color("#2a78d6");
const C_FOCUS = new THREE.Color("#eb6834");
const C_MAPPED = new THREE.Color("#0ca30c");
const C_PENDING = new THREE.Color("#fab219");

function discTexture(): THREE.Texture {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d")!;
  g.beginPath();
  g.arc(32, 32, 30, 0, Math.PI * 2);
  g.fillStyle = "#fff";
  g.fill();
  return new THREE.CanvasTexture(c);
}

export default function StarMap() {
  const host = useRef<HTMLDivElement>(null);
  const [fibo, setFibo] = useState<Fibo | null>(null);
  const [overlay, setOverlay] = useState<Overlay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [entity, setEntity] = useState<EntityDetail | null>(null);
  const [query, setQuery] = useState("");
  const [module, setModule] = useState("all");
  const [showProps, setShowProps] = useState(false);
  const flyTo = useRef<(i: number) => void>(() => undefined);

  useEffect(() => {
    Promise.all([api.get<Fibo>("/ontology/fibo"), api.get<Overlay>("/ontology/overlay")])
      .then(([f, o]) => {
        setFibo(f);
        setOverlay(o);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const byIri = useMemo(() => {
    const m = new Map<string, number>();
    fibo?.nodes.forEach((n, i) => n.iri && m.set(n.iri, i));
    return m;
  }, [fibo]);

  const entitiesAt = useMemo(() => {
    const m = new Map<number, Entity[]>();
    overlay?.entities.forEach((e) => {
      const i = e.concept ? byIri.get(e.concept) : undefined;
      if (i !== undefined) m.set(i, [...(m.get(i) ?? []), e]);
    });
    return m;
  }, [overlay, byIri]);

  // ---------------------------------------------------------------- three.js scene
  useEffect(() => {
    if (!fibo || !overlay || !host.current) return;
    const el = host.current;
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(el.clientWidth, el.clientHeight);
    renderer.setClearColor("#ffffff");
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(55, el.clientWidth / el.clientHeight, 1, 20000);
    camera.position.set(0, 0, 2600);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;

    const visible = fibo.nodes.map((n) => (n.k === "P" ? showProps : true) && n.k !== "O");
    const n = fibo.nodes.length;
    const pos = new Float32Array(n * 3);
    const col = new Float32Array(n * 3);
    fibo.nodes.forEach((node, i) => {
      const [x, y, z] = fibo.pos[i];
      pos.set(visible[i] ? [x, y, z] : [1e6, 1e6, 1e6], i * 3);
      const c = node.k === "K" || node.m === "KB" ? C_KB : module !== "all" && node.m === module ? C_FOCUS : C_FIBO;
      col.set([c.r, c.g, c.b], i * 3);
    });
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    const disc = discTexture();
    const stars = new THREE.Points(
      geo,
      new THREE.PointsMaterial({ size: 9, vertexColors: true, sizeAttenuation: true, map: disc, alphaTest: 0.5 }),
    );
    scene.add(stars);

    const seg: number[] = [];
    fibo.edges.forEach(([a, b, k]) => {
      if (k === 0 || !visible[a] || !visible[b]) return; // containment is implied by position; draw meaning edges
      seg.push(...fibo.pos[a], ...fibo.pos[b]);
    });
    const lines = new THREE.LineSegments(
      new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(seg, 3)),
      new THREE.LineBasicMaterial({ color: "#8a93a3", transparent: true, opacity: 0.12 }),
    );
    scene.add(lines);

    // overlay: BU entities as larger stars around the concept they map to (deterministic offset per IRI)
    const ov: { e: Entity; p: THREE.Vector3 }[] = [];
    entitiesAt.forEach((list, i) => {
      list.forEach((e, j) => {
        const [x, y, z] = fibo.pos[i];
        const a = (j / Math.max(1, list.length)) * Math.PI * 2;
        ov.push({ e, p: new THREE.Vector3(x + Math.cos(a) * 28, y + Math.sin(a) * 28, z + 14) });
      });
    });
    const ovGeo = new THREE.BufferGeometry().setFromPoints(ov.map((o) => o.p));
    const ovCol = new Float32Array(ov.length * 3);
    ov.forEach((o, i) => {
      const c = o.e.match === "pending" ? C_PENDING : C_MAPPED;
      ovCol.set([c.r, c.g, c.b], i * 3);
    });
    ovGeo.setAttribute("color", new THREE.BufferAttribute(ovCol, 3));
    const ovPts = new THREE.Points(ovGeo, new THREE.PointsMaterial({ size: 18, vertexColors: true, map: disc, alphaTest: 0.5 }));
    scene.add(ovPts);

    const marker = new THREE.Mesh(
      new THREE.RingGeometry(22, 28, 32),
      new THREE.MeshBasicMaterial({ color: "#b3261e", side: THREE.DoubleSide }),
    );
    marker.visible = false;
    scene.add(marker);

    const ray = new THREE.Raycaster();
    ray.params.Points = { threshold: 12 };
    const mouse = new THREE.Vector2();
    let down = { x: 0, y: 0 };
    const onDown = (e: PointerEvent) => (down = { x: e.clientX, y: e.clientY });
    const onUp = (e: PointerEvent) => {
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) return; // a drag, not a click
      const r = renderer.domElement.getBoundingClientRect();
      mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(mouse, camera);
      const hitOv = ray.intersectObject(ovPts)[0];
      if (hitOv?.index !== undefined) {
        const o = ov[hitOv.index];
        const i = o.e.concept ? byIri.get(o.e.concept) : undefined;
        if (i !== undefined) setSelected(i);
        openEntity(o.e.iri);
        return;
      }
      const hit = ray.intersectObject(stars).find((h) => h.index !== undefined && visible[h.index]);
      if (hit?.index !== undefined) setSelected(hit.index);
    };
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointerup", onUp);

    flyTo.current = (i: number) => {
      const [x, y, z] = fibo.pos[i];
      const target = new THREE.Vector3(x, y, z);
      controls.target.copy(target);
      camera.position.copy(target.clone().add(new THREE.Vector3(0, 0, 420)));
      marker.position.copy(target);
      marker.visible = true;
    };

    const onResize = () => {
      camera.aspect = el.clientWidth / el.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(el.clientWidth, el.clientHeight);
    };
    window.addEventListener("resize", onResize);
    let raf = 0;
    const tick = () => {
      controls.update();
      marker.lookAt(camera.position);
      renderer.render(scene, camera);
      raf = requestAnimationFrame(tick);
    };
    tick();
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      renderer.domElement.removeEventListener("pointerdown", onDown);
      renderer.domElement.removeEventListener("pointerup", onUp);
      controls.dispose();
      [geo, lines.geometry, ovGeo, marker.geometry].forEach((g) => g.dispose());
      disc.dispose();
      renderer.dispose();
      el.removeChild(renderer.domElement);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fibo, overlay, entitiesAt, module, showProps]);

  useEffect(() => {
    if (selected !== null) flyTo.current(selected);
  }, [selected]);

  function openEntity(iri: string) {
    setEntity(null);
    api
      .get<EntityDetail>(`/ontology/entity?iri=${encodeURIComponent(iri)}`)
      .then(setEntity)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!fibo || q.length < 2) return [];
    return fibo.nodes
      .map((node, i) => ({ node, i }))
      .filter(({ node }) => node.k !== "O" && (node.l.toLowerCase().includes(q) || node.n.toLowerCase().includes(q)))
      .map((r) => ({ ...r, own: entitiesAt.get(r.i)?.length ?? 0 }))
      .sort((a, b) => b.own - a.own || a.node.l.length - b.node.l.length) // concepts your BUs use first
      .slice(0, 12);
  }, [fibo, query, entitiesAt]);

  if (error) return <div className="error">{error}</div>;
  if (!fibo || !overlay) return <div className="muted">Loading the FIBO star map…</div>;
  const sel = selected !== null ? fibo.nodes[selected] : null;
  const unmapped = overlay.entities.filter((e) => e.match === "unmapped" || (e.concept && !byIri.has(e.concept)));
  const mapped = (overlay.counts.exact ?? 0) + (overlay.counts.accepted ?? 0);

  return (
    <div>
      <h1>Ontology star map</h1>
      <p className="muted" data-testid="starmap-stats">
        FIBO {fibo.release}: {fibo.counts.C?.toLocaleString()} classes · {fibo.counts.P?.toLocaleString()} properties ·{" "}
        {fibo.counts.K ?? 0} KB local concepts. Your BUs ({overlay.bus.join(", ") || "none"}):{" "}
        {overlay.entities.length} entities, {mapped} mapped, {overlay.counts.pending ?? 0} pending review,{" "}
        {overlay.counts.unmapped ?? 0} unmapped.
      </p>
      <div className="row" style={{ marginBottom: 8 }}>
        <div className="search-box">
          <input
            placeholder="Search FIBO and KB concepts…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            data-testid="starmap-search"
          />
          {results.length > 0 && (
            <div className="results">
              {results.map(({ node, i, own }) => (
                <button
                  key={i}
                  data-own={own}
                  onClick={() => {
                    setSelected(i);
                    setQuery("");
                  }}
                >
                  {node.l}{" "}
                  <span className="muted">
                    {KIND[node.k]} · {node.m}
                    {own > 0 ? ` · ${own} of your entities` : ""}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        <label className="field">
          Highlight module
          <select value={module} onChange={(e) => setModule(e.target.value)} data-testid="starmap-module">
            <option value="all">none</option>
            {fibo.modules.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}: {m.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input type="checkbox" checked={showProps} onChange={(e) => setShowProps(e.target.checked)} /> properties
        </label>
        <span className="legend-inline">
          <span><i style={{ background: "#b4bac6" }} /> FIBO</span>
          <span><i style={{ background: "#2a78d6" }} /> KB local concept</span>
          {module !== "all" && <span><i style={{ background: "#eb6834" }} /> {module}</span>}
          <span><i className="big" style={{ background: "#0ca30c" }} /> ✓ BU entity, mapped</span>
          <span><i className="big" style={{ background: "#fab219" }} /> ! BU entity, pending review</span>
        </span>
      </div>
      <div className="starmap">
        <div className="stage" ref={host} data-testid="starmap-canvas" />
        <aside className="drawer" data-testid="starmap-drawer">
          {!sel ? (
            <p className="muted">Click a star, or search. Drag to rotate, scroll to zoom.</p>
          ) : (
            <>
              <div className="muted">
                {KIND[sel.k]} · {sel.m}
              </div>
              <h2 style={{ marginTop: 4 }}>{sel.l}</h2>
              {sel.iri && <div className="iri">{sel.iri}</div>}
              {sel.d && <p>{sel.d}</p>}
              <h3>Relations</h3>
              {fibo.edges
                .filter(([a, b, k]) => k !== 0 && (a === selected || b === selected))
                .slice(0, 14)
                .map(([a, b, k], j) => {
                  const other = a === selected ? b : a;
                  return (
                    <div key={j}>
                      <span className="muted">{a === selected ? EDGE[k] : `← ${EDGE[k]}`}</span>{" "}
                      <button className="link" onClick={() => setSelected(other)}>
                        {fibo.nodes[other].l}
                      </button>
                    </div>
                  );
                })}
              <h3>Your BU entities here</h3>
              {(entitiesAt.get(selected!) ?? []).length === 0 ? (
                <p className="muted">None.</p>
              ) : (
                (entitiesAt.get(selected!) ?? []).map((e) => (
                  <div key={e.iri}>
                    <button className="link" onClick={() => openEntity(e.iri)}>
                      {e.label}
                    </button>{" "}
                    <span className={`chip ${e.match === "pending" ? "warn" : "ok"}`}>
                      {e.match === "pending" ? `! pending ${e.confidence?.toFixed(2) ?? ""}` : `✓ ${e.match}`}
                    </span>{" "}
                    <span className="muted">{e.bu}</span>
                  </div>
                ))
              )}
            </>
          )}
          {entity && (
            <div className="panel" style={{ marginTop: 12 }} data-testid="starmap-entity">
              <b>{entity.label}</b> <span className="muted">({entity.bu})</span>
              <h3>Source documents</h3>
              {entity.sources.length === 0 ? (
                <p className="muted">None you can open.</p>
              ) : (
                entity.sources.map((s) => (
                  <div key={s.doc_id}>
                    <Link to={`/documents/${s.doc_id}`}>{s.title}</Link> <span className="muted">v{s.version_no}</span>
                  </div>
                ))
              )}
              <h3>Mart columns</h3>
              {entity.mart_hints.length === 0 ? (
                <p className="muted">None in tables you can query.</p>
              ) : (
                entity.mart_hints.map((h) => <div key={h}>{h}</div>)
              )}
            </div>
          )}
          {unmapped.length > 0 && (
            <details style={{ marginTop: 12 }}>
              <summary>Unmapped entities ({unmapped.length})</summary>
              {unmapped.slice(0, 200).map((e) => (
                <div key={e.iri}>
                  <button className="link" onClick={() => openEntity(e.iri)}>
                    {e.label}
                  </button>{" "}
                  <span className="muted">{e.bu}</span>
                </div>
              ))}
            </details>
          )}
        </aside>
      </div>
    </div>
  );
}
