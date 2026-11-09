"""FIBO star map: every module of the pinned FIBO release (plus our local extension concepts) as a 3D graph.

Builds a compact JSON artefact for the UI's ontology page:
  nodes  [{k, n, l, m, iri, d}]   k: M module | O ontology | C class | P property | K local concept (no individuals)
  edges  [[source, target, kind]] kind: 0 contains, 1 subclass-of, 2 domain, 3 range, 4 subproperty-of, 5 type,
                                  6 broader (local concept -> FIBO class)
  pos    [[x, y, z]]              integers, a deterministic structural layout (reruns converge)
The FIBO part is public (EDM Council, MIT licence) and holds no bank data, so it is served to any signed-in user.
BU entities are NOT in this file: they come from the gateway's overlay endpoint, scoped to the caller's BUs.

Usage: python -m kb.graph.starmap [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from kb.config import REPO_ROOT
from kb.graph.fibo import FIBO_RELEASE, FIBO_ROOT, FIBO_SHA256

ALL_MODULES = ("FND", "BE", "FBC", "SEC", "DER", "LOAN", "IND", "BP", "CAE", "MD", "ACTUS")
MODULE_LABELS = {
    "FND": "Foundations", "BE": "Business Entities", "FBC": "Financial Business and Commerce", "SEC": "Securities",
    "DER": "Derivatives", "LOAN": "Loans", "IND": "Indices and Indicators", "BP": "Business Process",
    "CAE": "Corporate Actions and Events", "MD": "Market Data", "ACTUS": "ACTUS", "KB": "KB local extension",
}  # fmt: skip
OUT = REPO_ROOT / "data" / "ontology" / "starmap" / f"fibo-{FIBO_RELEASE}.json"
FIBO_NS = "https://spec.edmcouncil.org/fibo/ontology/"
E_CONTAINS, E_SUBCLASS, E_DOMAIN, E_RANGE, E_SUBPROP, E_TYPE, E_BROADER = range(7)


def _label(g: Any, s: Any) -> str | None:
    from rdflib.namespace import RDFS

    for v in g.objects(s, RDFS.label):
        if getattr(v, "language", None) in (None, "en"):
            return str(v)
    return None


def _definition(g: Any, s: Any) -> str:
    from rdflib.namespace import SKOS

    for v in g.objects(s, SKOS.definition):
        return str(v)[:500]
    return ""


def _spiral(count: int, radius: float, centre: tuple[float, float, float]) -> list[tuple[float, float, float]]:
    """``count`` points spread evenly on a sphere (golden spiral) around ``centre``."""
    cx, cy, cz = centre
    out = []
    for j in range(count):
        phi = math.acos(1 - 2 * (j + 0.5) / count)
        theta = math.pi * (1 + 5**0.5) * j
        out.append((cx + radius * math.sin(phi) * math.cos(theta), cy + radius * math.sin(phi) * math.sin(theta),
                    cz + radius * math.cos(phi)))  # fmt: skip
    return out


def layout(nodes: list[dict[str, Any]], edges: set[tuple[int, int, int]], mods: list[str]) -> list[list[int]]:
    """Deterministic structural layout, linear in the node count (a force layout took > 10 min on ~5k nodes):
    modules on a sphere, each module's ontologies on a shell around it, each ontology's members on a small
    shell around the ontology, local concepts beside their broader FIBO class. Same input -> same picture."""
    pos: dict[int, tuple[float, float, float]] = {}
    mod_idx = {n["n"]: i for i, n in enumerate(nodes) if n["k"] == "M"}
    for m, p in zip(mods, _spiral(len(mods), 1000.0, (0.0, 0.0, 0.0)), strict=True):
        pos[mod_idx[m]] = p
    children: dict[int, list[int]] = {}
    for a, b, k in sorted(edges):
        if k == E_CONTAINS:
            children.setdefault(a, []).append(b)
    broader = {a: b for a, b, k in edges if k == E_BROADER}
    for m in mods:
        mi = mod_idx[m]
        kids = [c for c in children.get(mi, []) if c not in broader]
        onts = [c for c in kids if nodes[c]["k"] == "O"]
        loose = [c for c in kids if nodes[c]["k"] != "O"]
        shell = 140.0 + 12.0 * math.sqrt(len(onts) + len(loose))
        for c, p in zip(onts + loose, _spiral(len(onts) + len(loose), shell, pos[mi]), strict=True):
            pos[c] = p
        for o in onts:
            members = children.get(o, [])
            r = 18.0 + 5.0 * math.sqrt(len(members))
            for c, p in zip(members, _spiral(len(members), r, pos[o]), strict=True):
                pos[c] = p
    for a, b in sorted(broader.items()):  # local concept: just outside its broader class
        if b in pos:
            x, y, z = pos[b]
            n = max(1.0, math.sqrt(x * x + y * y + z * z))
            pos[a] = (x + 30 * x / n, y + 30 * y / n, z + 30 * z / n)
    for i in range(len(nodes)):  # anything unplaced (no container) sits at its module
        pos.setdefault(i, pos[mod_idx[nodes[i]["m"]]])
    scale = 1000.0 / max(max(abs(v) for v in p) for p in pos.values())
    return [[round(v * scale) for v in pos[i]] for i in range(len(nodes))]


def build(root: Path = FIBO_ROOT, modules: tuple[str, ...] = ALL_MODULES) -> dict[str, Any]:
    from rdflib import Graph, URIRef
    from rdflib.namespace import OWL, RDF, RDFS

    from kb.graph.ontology import Ontology

    t0 = time.monotonic()
    g = Graph()
    files = 0
    for mod in modules:
        for f in sorted((root / mod).rglob("*.rdf")):
            g.parse(f, format="xml")
            files += 1

    nodes: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def add(key: str, node: dict[str, Any]) -> int:
        if key not in index:
            index[key] = len(nodes)
            nodes.append(node)
        return index[key]

    for mod in (*modules, "KB"):
        add(f"module:{mod}", {"k": "M", "n": mod, "l": MODULE_LABELS[mod], "m": mod})
    for ont in sorted(str(o) for o in g.subjects(RDF.type, OWL.Ontology) if str(o).startswith(FIBO_NS)):
        mod = ont[len(FIBO_NS) :].split("/")[0]
        if mod in modules:
            add(ont, {"k": "O", "n": ont.rstrip("/").rsplit("/", 1)[-1], "l": _label(g, URIRef(ont)) or "", "m": mod,
                      "iri": ont, "d": ""})  # fmt: skip

    # individuals are left out: ~11.5k code-list entries (jurisdictions, currencies) that swamp navigation
    kinds = ((OWL.Class, "C"), (OWL.ObjectProperty, "P"), (OWL.DatatypeProperty, "P"))
    for rdf_type, k in kinds:
        for s in sorted({x for x in g.subjects(RDF.type, rdf_type) if isinstance(x, URIRef)}, key=str):
            iri = str(s)
            if not iri.startswith(FIBO_NS) or iri in index:
                continue
            mod = iri[len(FIBO_NS) :].split("/")[0]
            label = _label(g, s)
            if mod not in modules or not label:
                continue
            add(iri, {"k": k, "n": iri.rsplit("/", 1)[-1], "l": label, "m": mod, "iri": iri, "d": _definition(g, s)})

    edges: set[tuple[int, int, int]] = set()
    onts = sorted((i for i, n in enumerate(nodes) if n["k"] == "O"), key=lambda i: -len(nodes[i]["iri"]))
    for i, n in enumerate(nodes):
        if n["k"] == "O":
            edges.add((index[f"module:{n['m']}"], i, E_CONTAINS))
        elif n["k"] in ("C", "P", "I"):
            home = next((o for o in onts if n["iri"].startswith(nodes[o]["iri"])), None)
            edges.add((home if home is not None else index[f"module:{n['m']}"], i, E_CONTAINS))
    for pred, kind in ((RDFS.subClassOf, E_SUBCLASS), (RDFS.subPropertyOf, E_SUBPROP), (RDFS.domain, E_DOMAIN),
                       (RDFS.range, E_RANGE), (RDF.type, E_TYPE)):  # fmt: skip
        for subj, obj in g.subject_objects(pred):
            a, b = index.get(str(subj)), index.get(str(obj))
            target_kind = "P" if kind == E_SUBPROP else "C"
            if a is not None and b is not None and a != b and nodes[b]["k"] == target_kind:
                edges.add((a, b, kind))

    ontology = Ontology()
    for c in sorted(ontology.concepts.values(), key=lambda c: c.iri):
        if not c.local:
            continue
        i = add(c.iri, {"k": "K", "n": c.iri.rsplit("/", 1)[-1], "l": c.label, "m": "KB", "iri": c.iri,
                        "d": c.definition[:500], "bm": c.module})  # fmt: skip
        edges.add((index["module:KB"], i, E_CONTAINS))
        if c.broader and c.broader in index:
            edges.add((i, index[c.broader], E_BROADER))

    mods = [n["n"] for n in nodes if n["k"] == "M"]
    out_pos = layout(nodes, edges, mods)

    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["k"]] = counts.get(n["k"], 0) + 1
    return {
        "release": FIBO_RELEASE,
        "sha256": FIBO_SHA256,
        "modules": [{"id": m, "label": MODULE_LABELS[m]} for m in mods],
        "counts": counts,
        "files": files,
        "nodes": nodes,
        "edges": sorted(edges),
        "pos": out_pos,
        "build_seconds": round(time.monotonic() - t0, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.graph.starmap")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    if not FIBO_ROOT.exists():
        raise SystemExit(f"FIBO {FIBO_RELEASE} not found at {FIBO_ROOT}: run `kb.ps1 models`")
    data = build()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    a.out.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    summary = {"out": str(a.out), "bytes": len(body), "sha256_16": digest, "counts": data["counts"]}
    print(json.dumps(summary | {"edges": len(data["edges"]), "files": data["files"], "seconds": data["build_seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
