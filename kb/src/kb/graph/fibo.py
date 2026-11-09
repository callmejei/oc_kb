"""FIBO label index: the classes of the modules we use, with labels, synonyms and definitions.

FIBO (EDM Council, MIT licence) is downloaded once, pinned by SHA-256 (kbinfra `models`), and read offline.
The index is cached as JSON next to the release so normal runs never re-parse ~200 RDF/XML files.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kb.config import REPO_ROOT

FIBO_RELEASE = "master_2026Q3"
FIBO_SHA256 = "46385076935d28e65c3e3a4b68b78416948e5ca1afbd0dfe312c3fe8a0010694"
FIBO_URL = f"https://github.com/edmcouncil/fibo/archive/refs/tags/{FIBO_RELEASE}.zip"
MODULES = ("FND", "BE", "FBC", "LOAN", "SEC")
FIBO_ROOT = REPO_ROOT / ".infra" / "ontology" / "fibo" / f"fibo-{FIBO_RELEASE}"
INDEX_PATH = REPO_ROOT / ".infra" / "ontology" / f"fibo-{FIBO_RELEASE}-index.json"

SYNONYM_PROPS = (
    "http://www.w3.org/2004/02/skos/core#altLabel",
    "https://www.omg.org/spec/Commons/AnnotationVocabulary/synonym",
    "https://spec.edmcouncil.org/fibo/ontology/FND/Utilities/AnnotationVocabulary/synonym",
    "https://www.omg.org/spec/Commons/AnnotationVocabulary/abbreviation",
    "https://spec.edmcouncil.org/fibo/ontology/FND/Utilities/AnnotationVocabulary/abbreviation",
)


def norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


@dataclass
class FiboClass:
    iri: str
    label: str
    synonyms: list[str] = field(default_factory=list)
    definition: str = ""
    module: str = ""

    @property
    def curie(self) -> str:
        m = re.match(r"https://spec\.edmcouncil\.org/fibo/ontology/([A-Z]+)/(.+)/([^/]+)$", self.iri)
        return f"fibo-{m.group(1).lower()}:{m.group(3)}" if m else self.iri


def build_index(root: Path = FIBO_ROOT) -> list[FiboClass]:
    from rdflib import Graph, URIRef
    from rdflib.namespace import OWL, RDF, RDFS, SKOS

    g = Graph()
    for mod in MODULES:
        for f in sorted((root / mod).rglob("*.rdf")):
            g.parse(f, format="xml")
    out: list[FiboClass] = []
    for cls in set(g.subjects(RDF.type, OWL.Class)):
        if not isinstance(cls, URIRef) or not str(cls).startswith("https://spec.edmcouncil.org/fibo/"):
            continue
        labels = [str(v) for v in g.objects(cls, RDFS.label) if getattr(v, "language", None) in (None, "en")]
        if not labels:
            continue
        syns = sorted({str(v) for p in SYNONYM_PROPS for v in g.objects(cls, URIRef(p))})
        defs = [str(v) for v in g.objects(cls, SKOS.definition)]
        mod = str(cls).split("/ontology/")[1].split("/")[0]
        out.append(FiboClass(str(cls), labels[0], syns, defs[0] if defs else "", mod))
    return sorted(out, key=lambda c: c.iri)


def load_index(rebuild: bool = False) -> list[FiboClass]:
    if INDEX_PATH.exists() and not rebuild:
        return [FiboClass(**d) for d in json.loads(INDEX_PATH.read_text(encoding="utf-8"))["classes"]]
    if not FIBO_ROOT.exists():
        raise FileNotFoundError(f"FIBO {FIBO_RELEASE} not found at {FIBO_ROOT}: run `kb.ps1 models`")
    classes = build_index()
    INDEX_PATH.write_text(
        json.dumps({"release": FIBO_RELEASE, "sha256": FIBO_SHA256, "classes": [asdict(c) for c in classes]}, indent=0),
        encoding="utf-8",
    )
    return classes
