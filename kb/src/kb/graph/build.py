"""Ontology build, one BU at a time (graphify-fibo skill, steps 1-8).

export current-version Markdown (SharePoint sources only) -> graphify (guarded subprocess) -> RDF with
provenance -> FIBO mapping (auto-accept or review queue) -> term spotting on chunks -> SHACL -> atomic swap
of urn:kb:graph:<bu> -> concept_ids written back to the index (no re-embedding).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdflib import RDF, RDFS, SKOS, XSD, Graph, Literal, Namespace, URIRef

from kb.adapters.local.graph_store import OxigraphGraphStore
from kb.adapters.local.registry import PostgresRegistry
from kb.config import REPO_ROOT
from kb.graph.fibo import FIBO_RELEASE, norm_label
from kb.graph.graphify_runner import GraphifyRun, run_graphify
from kb.graph.mapping import Mapper, Mapping
from kb.graph.ontology import Ontology

V = Namespace("https://kb.local/vocab#")
ONTOLOGY_GRAPH = "urn:kb:graph:ontology"
MARKER = URIRef("urn:kb:marker")
SHAPES = REPO_ROOT / "shapes" / "kb-shapes.ttl"
ACTOR = "svc:ontology"


def graph_iri(bu: str) -> str:
    return f"urn:kb:graph:{bu}"


def entity_iri(bu: str, label: str) -> URIRef:
    return URIRef(f"https://kb.local/id/{bu}/{hashlib.sha256(f'{bu}|{norm_label(label)}'.encode()).hexdigest()[:16]}")


def chunk_iri(chunk_id: str) -> URIRef:
    return URIRef(f"urn:kb:chunk:{chunk_id}")


def version_iri(version_id: str) -> URIRef:
    return URIRef(f"urn:kb:version:{version_id}")


PART_SEP = "__p"


def split_markdown(md: str, max_chars: int) -> list[str]:
    """Split a document's Markdown into parts of about `max_chars` at heading, else blank-line, boundaries.

    qwen3-8B degenerates into repetition on long extraction outputs (DECISIONS D-04x); shorter inputs keep
    each response short. Blocks (tables, lists) are never cut: a block larger than max_chars stays whole.
    """
    blocks: list[str] = []
    for para in md.split("\n\n"):
        if para.lstrip().startswith("#") or not blocks:
            blocks.append(para)
        else:
            blocks[-1] += "\n\n" + para
    # sections -> paragraphs when a section alone is too big
    units: list[str] = []
    for b in blocks:
        units.extend(b.split("\n\n") if len(b) > max_chars else [b])
    parts: list[str] = []
    for u in units:
        if parts and len(parts[-1]) + len(u) + 2 <= max_chars:
            parts[-1] += "\n\n" + u
        else:
            parts.append(u)
    return [x for x in parts if x.strip()] or [md]


def degenerate(label: str) -> bool:
    """A label produced by an LLM repetition loop ('X and Documentation and Documentation ...')."""
    words = [w for w in label.lower().replace("_", " ").split() if w.isalnum()]
    return len(label) > 100 or (len(words) >= 6 and len(set(words)) * 2 <= len(words))


def doc_id_of(source_file: str) -> str:
    stem = Path(source_file.replace("\\", "/")).stem
    head, sep, tail = stem.rpartition(PART_SEP)
    return head if sep and tail.isdigit() else stem


class ShaclError(RuntimeError):
    def __init__(self, report: str) -> None:
        super().__init__(report[:2000])
        self.report = report


@dataclass
class BuReport:
    bu: str
    documents: int = 0
    chunks: int = 0
    entities: int = 0
    concept_entities: int = 0
    auto_accepted: int = 0
    review_accepted: int = 0
    queued: int = 0
    relations: int = 0
    tagged_chunks: int = 0
    triples: int = 0
    graphify_seconds: float = 0.0
    dropped_degenerate: int = 0
    mappings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return (self.auto_accepted + self.review_accepted) / self.concept_entities if self.concept_entities else 1.0


def validate(data: Graph, ontology_graph: Graph) -> None:
    from pyshacl import validate as shacl

    combined = Graph()
    for t in data:
        combined.add(t)
    for t in ontology_graph:
        combined.add(t)
    conforms, _g, text = shacl(combined, shacl_graph=str(SHAPES), inference="none", advanced=True, allow_warnings=False)
    if not conforms:
        raise ShaclError(text)


class OntologyBuilder:
    def __init__(
        self,
        *,
        registry: PostgresRegistry,
        index: Any,
        processed: Any,
        store: OxigraphGraphStore,
        ontology: Ontology,
        mapper: Mapper,
        llm_base_url: str,
        llm_model: str,
        allowed_hosts: list[str],
        runs_dir: Path,
        token_budget: int = 4000,
        work_dir: Path | None = None,
        graphify_opts: dict[str, int] | None = None,
    ) -> None:
        self.reg, self.index, self.processed, self.store = registry, index, processed, store
        self.o, self.mapper = ontology, mapper
        self.llm_base_url, self.llm_model, self.allowed_hosts = llm_base_url, llm_model, allowed_hosts
        self.runs_dir, self.token_budget = runs_dir, token_budget
        # per-BU graphify workspace; out/ holds graphify's semantic cache so reruns resume (never shared across BUs)
        self.work_dir = work_dir or runs_dir.parent / "work"
        self.graphify_opts = dict(graphify_opts or {})
        self.part_chars = self.graphify_opts.pop("part_chars", 2000)

    # ------------------------------------------------------------------ ontology graph (global)
    def ontology_graph(self) -> Graph:
        g = Graph()
        for c in self.o.concepts.values():
            s = URIRef(c.iri)
            g.add((s, RDFS.label, Literal(c.label)))
            g.add((s, V.module, Literal(c.module)))
            g.add((s, V.curie, Literal(c.curie)))
            for t in dict.fromkeys(c.terms):
                g.add((s, SKOS.altLabel, Literal(t)))
            if c.broader:
                g.add((s, RDFS.subClassOf, URIRef(c.broader)))
        return g

    def publish_ontology(self) -> int:
        g = self.ontology_graph()
        self.store.replace_graph(ONTOLOGY_GRAPH, iter(g.serialize(format="nt").splitlines()))
        return len(g)

    # ------------------------------------------------------------------ inputs
    def _documents(self, bu: str) -> list[dict[str, Any]]:
        with self.reg.transaction(ACTOR) as c:
            return c.execute(
                """SELECT d.doc_id, d.title, v.version_id FROM kb.document d
                   JOIN kb.kb_source s USING (source_id)
                   JOIN kb.document_version v ON v.doc_id = d.doc_id AND v.is_current AND v.status = 'INDEXED'
                   WHERE s.bu = %s AND s.source_type = 'SHAREPOINT' AND s.status = 'ACTIVE' AND d.status = 'ACTIVE'
                   ORDER BY d.doc_id""",
                (bu,),
            ).fetchall()

    def _chunks(self, version_ids: list[str]) -> list[dict[str, Any]]:
        if not version_ids:
            return []
        rows: list[dict[str, Any]] = self.index.docs(
            {"bool": {"filter": [{"terms": {"version_id": version_ids}}, {"term": {"is_current": True}}]}}, size=5000
        )
        return rows

    def _export_markdown(self, docs: list[dict[str, Any]], out: Path) -> None:
        from docling_core.types.doc.document import DoclingDocument

        for d in docs:
            key = f"{d['version_id'].replace(':', '_')}/docling.json"
            with self.processed.open(self.processed.uri_for(key)) as f:
                doc = DoclingDocument.model_validate(json.loads(f.read()))
            for i, part in enumerate(split_markdown(doc.export_to_markdown(), self.part_chars), 1):
                (out / f"{d['doc_id']}{PART_SEP}{i}.md").write_text(part, encoding="utf-8")

    def _accepted_reviews(self, bu: str) -> dict[str, str]:
        with self.reg.transaction(ACTOR) as c:
            rows = c.execute(
                "SELECT entity_iri, proposed_concept FROM kb.concept_review WHERE bu = %s AND status = 'ACCEPTED'",
                (bu,),
            ).fetchall()
        return {r["entity_iri"]: r["proposed_concept"] for r in rows}

    def _queue(
        self, bu: str, run_id: str, ent: URIRef, label: str, m: Mapping, chunks: list[str], context: str
    ) -> None:
        assert m.concept
        with self.reg.transaction(ACTOR) as c:
            c.execute(
                """INSERT INTO kb.concept_review (bu, entity_iri, entity_label, proposed_concept, proposed_label,
                                                  confidence, method, chunk_ids, context, run_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (bu, entity_iri, proposed_concept) DO NOTHING""",
                (
                    bu,
                    str(ent),
                    label,
                    m.concept,
                    self.o.concepts[m.concept].label,
                    m.confidence,
                    m.method,
                    chunks[:10],
                    context[:500],
                    run_id,
                ),
            )

    # ------------------------------------------------------------------ build one BU
    def build_bu(self, bu: str, run_id: str | None = None, graphify_out: Path | None = None) -> BuReport:
        run_id = run_id or time.strftime("%Y%m%d-%H%M%S")  # same scheme as build_all
        rep = BuReport(bu)
        docs = self._documents(bu)
        rep.documents = len(docs)
        chunks = self._chunks([d["version_id"] for d in docs])
        rep.chunks = len(chunks)
        by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ch in chunks:
            by_doc[ch["doc_id"]].append(ch)
        version_of = {d["doc_id"]: d["version_id"] for d in docs}
        run_dir = self.runs_dir / run_id / bu
        run_dir.mkdir(parents=True, exist_ok=True)

        # 1. extract (one BU per graphify run)
        if graphify_out is None:
            work = self.work_dir / bu
            md = work / "md"
            shutil.rmtree(md, ignore_errors=True)  # current versions only, rewritten each run
            md.mkdir(parents=True)
            self._export_markdown(docs, md)
            g_run: GraphifyRun = run_graphify(
                md,
                work / "out",
                base_url=self.llm_base_url,
                model=self.llm_model,
                allowed_hosts=self.allowed_hosts,
                token_budget=self.token_budget,
                **self.graphify_opts,
            )
            shutil.copyfile(g_run.graph_path, run_dir / "graph.json")
            rep.graphify_seconds = round(g_run.seconds, 1)
        else:
            shutil.copyfile(graphify_out, run_dir / "graph.json")
        raw = json.loads((run_dir / "graph.json").read_text(encoding="utf-8"))

        # 2. normalise to RDF with provenance
        g = Graph()
        g.add((MARKER, V.graphBu, Literal(bu)))
        for mod in self.o.allowed_modules[bu]:
            g.add((MARKER, V.allowedModule, Literal(mod)))
        node_iri: dict[str, URIRef] = {}
        accepted_reviews = self._accepted_reviews(bu)
        entity_chunks: dict[URIRef, set[str]] = defaultdict(set)
        th = self.o.thresholds["auto_accept"]
        for n in raw.get("nodes", []):
            doc_id = doc_id_of(str(n.get("source_file", "")))
            if doc_id not in version_of:
                continue  # never attach a node to a document outside this BU's current set
            label = str(n.get("label") or n.get("id"))
            if degenerate(label):
                rep.dropped_degenerate += 1  # LLM repetition debris; edges to it are dropped with it
                continue
            # graphify's file_type is the node's source category (code|document|paper|image|rationale|concept),
            # not "this node is the file": every node is an entity
            ent = entity_iri(bu, label)
            node_iri[n["id"]] = ent
            cids = self._resolve_chunks(by_doc.get(doc_id, []), str(n.get("source_location") or ""), label)
            entity_chunks[ent] |= set(cids)
            if (ent, RDF.type, V.Entity) in g:
                g.add((ent, V.fromVersion, version_iri(version_of[doc_id])))
                continue
            rep.entities += 1
            rep.concept_entities += 1
            g.add((ent, RDF.type, V.Entity))
            g.add((ent, RDFS.label, Literal(label, datatype=XSD.string)))
            g.add((ent, V.bu, Literal(bu)))
            g.add((ent, V.fromVersion, version_iri(version_of[doc_id])))
            m = self.mapper.map(label, bu)
            rep.mappings.append(
                {
                    "label": label,
                    "concept": self.o.concepts[m.concept].curie if m.concept else None,
                    "confidence": m.confidence,
                    "method": m.method,
                }
            )
            if m.accepted(th):
                g.add((ent, V.concept, URIRef(m.concept)))  # type: ignore[arg-type]
                g.add((ent, SKOS.exactMatch, URIRef(m.concept)))  # type: ignore[arg-type]
                rep.auto_accepted += 1
            elif str(ent) in accepted_reviews:
                c = accepted_reviews[str(ent)]
                g.add((ent, V.concept, URIRef(c)))
                g.add((ent, SKOS.closeMatch, URIRef(c)))
                rep.review_accepted += 1
            elif m.concept:
                self._queue(bu, run_id, ent, label, m, cids, label)
                rep.queued += 1
        for ent, cset in entity_chunks.items():
            for cid in sorted(cset):
                g.add((ent, V.fromChunk, chunk_iri(cid)))
        for e in raw.get("edges", raw.get("links", [])):
            s, t = node_iri.get(e.get("source")), node_iri.get(e.get("target"))
            if not s or not t or (s, RDF.type, V.Entity) not in g or (t, RDF.type, V.Entity) not in g:
                continue
            rel = str(e.get("relation", "related_to"))
            r = URIRef(f"https://kb.local/id/{bu}/rel/{hashlib.sha256(f'{s}|{rel}|{t}'.encode()).hexdigest()[:16]}")
            g.add((r, RDF.type, V.Relation))
            g.add((r, V.source, s))
            g.add((r, V.target, t))
            g.add((r, V.relation, Literal(rel)))
            g.add((r, V.edgeTag, Literal(str(e.get("confidence", "EXTRACTED")))))
            g.add((r, V.bu, Literal(bu)))
            g.add((s, V.related, t))
            rep.relations += 1

        # 3. term spotting -> chunk mentions + concept_ids
        concepts: dict[str, list[str]] = {}
        entity_concepts = {str(ent): str(o) for ent, _, o in g.triples((None, V.concept, None))}
        chunk_entities: dict[str, set[str]] = defaultdict(set)
        for ent, cset in entity_chunks.items():
            for cid in cset:
                chunk_entities[cid].add(str(ent))
        for ch in chunks:
            spotted = self.o.spot(ch["content"], bu)
            from_entities = [
                entity_concepts[e] for e in chunk_entities.get(ch["chunk_id"], set()) if e in entity_concepts
            ]
            cs = list(dict.fromkeys([*spotted, *from_entities]))
            concepts[ch["chunk_id"]] = cs
            node = chunk_iri(ch["chunk_id"])
            g.add((node, RDF.type, V.Chunk))
            g.add((node, V.bu, Literal(bu)))
            g.add((node, V.fromVersion, version_iri(ch["version_id"])))
            for c in cs:
                g.add((node, V.mentions, URIRef(c)))
            rep.tagged_chunks += bool(cs)

        # 4. SHACL, 5. atomic swap
        validate(g, self.ontology_graph())
        rep.triples = len(g)
        (run_dir / "graph.ttl").write_text(g.serialize(format="turtle"), encoding="utf-8")
        self.store.replace_graph(graph_iri(bu), iter(g.serialize(format="nt").splitlines()))

        # 7. concept_ids on chunks (partial update, no re-embedding)
        self.index.set_concepts(concepts)
        report = {k: v for k, v in rep.__dict__.items()} | {
            "coverage": round(rep.coverage, 3),
            "fibo_release": FIBO_RELEASE,
            "run_id": run_id,
        }
        (run_dir / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        return rep

    @staticmethod
    def _resolve_chunks(chunks: list[dict[str, Any]], section: str, label: str) -> list[str]:
        """graphify gives a section heading, not a chunk: match by section path, else by label text."""
        sec = norm_label(section)
        if sec:
            hits = [c["chunk_id"] for c in chunks if sec and sec in norm_label(c.get("section_path") or "")]
            if hits:
                return hits
        lab = norm_label(label)
        hits = [c["chunk_id"] for c in chunks if lab and lab in norm_label(c.get("content") or "")]
        if hits:
            return hits
        return [chunks[0]["chunk_id"]] if chunks else []

    def build_all(self, bus: list[str] | None = None) -> dict[str, BuReport]:
        run_id = time.strftime("%Y%m%d-%H%M%S")
        self.publish_ontology()
        return {bu: self.build_bu(bu, run_id) for bu in (bus or list(self.o.allowed_modules))}


def build_builder(
    settings: Any,
    *,
    registry_dsn: str | None = None,
    index_alias: str | None = None,
    processed_url: str | None = None,
    store_path: str | None = None,
    runs_dir: str | None = None,
) -> OntologyBuilder:
    from kb.adapters import adapter_class, build
    from kb.ingest.factory import _port

    gcfg = settings.graph
    llm = settings.ports["llm"].options
    index = _port(settings, "chunk_index", **({"index_alias": index_alias} if index_alias else {}))
    ontology = Ontology()
    return OntologyBuilder(
        registry=PostgresRegistry(registry_dsn or settings.processing["registry_dsn"]),
        index=index,
        processed=_port(settings, "raw_store", url=processed_url or settings.processing["processed_url"]),
        store=adapter_class("local.OxigraphGraphStore")(
            path=store_path or settings.ports["graph_store"].options["path"]
        ),
        ontology=ontology,
        mapper=Mapper(ontology, build(settings, "embedder")),
        llm_base_url=str(llm["base_url"]),
        llm_model=str(gcfg.get("graphify_model", llm["model"])),
        allowed_hosts=settings.allowed_hosts,
        runs_dir=Path(runs_dir or settings.path("data_dir") / "ontology" / "runs"),
        token_budget=int(gcfg.get("graphify_token_budget", 4000)),
        graphify_opts={
            k: int(gcfg[k])
            for k in ("num_ctx", "max_output_tokens", "api_timeout_s", "timeout_s", "seed", "part_chars")
            if k in gcfg
        },
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from kb.config import load_settings

    ap = argparse.ArgumentParser(prog="kb.graph.build")
    ap.add_argument("--bu", action="append", help="build only these BUs (default: all)")
    ap.add_argument(
        "--from-graph",
        type=Path,
        help="re-normalise/map/load an existing graphify graph.json for one --bu, without calling the LLM",
    )
    a = ap.parse_args(argv)
    builder = build_builder(load_settings(strict=True))
    if a.from_graph:
        if not a.bu or len(a.bu) != 1:
            ap.error("--from-graph needs exactly one --bu (a graph never spans BUs)")
        reports = {a.bu[0]: builder.build_bu(a.bu[0], graphify_out=a.from_graph)}
    else:
        reports = builder.build_all(a.bu)
    for bu, r in reports.items():
        print(
            f"{bu:<10} docs={r.documents} entities={r.entities} auto={r.auto_accepted} review_ok={r.review_accepted} "
            f"queued={r.queued} coverage={r.coverage:.2f} relations={r.relations} tagged_chunks={r.tagged_chunks}/"
            f"{r.chunks} dropped_degenerate={r.dropped_degenerate} graphify={r.graphify_seconds}s"
        )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
