"""Graph expansion at query time (graphify-fibo skill, step 7).

1. Spot concepts in the question with the ontology's term index (global vocabulary, no BU data).
2. Find related accepted concepts by SPARQL over ONLY the caller's readable BU graphs (+ the global
   ontology graph): entity relations and chunk co-mentions. Other BUs' graphs are not in the query's
   dataset, so they cannot contribute anything.
3. Return expansion terms (synonyms/labels of the question's concepts, labels of related concepts) and
   concept ids. Retrieval then runs them through the same mandatory ACL pre-filter as BM25 and kNN.
Expansion never returns document text; it only steers retrieval.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from kb.graph.build import ONTOLOGY_GRAPH, graph_iri
from kb.graph.fibo import norm_label
from kb.graph.ontology import Ontology
from kb.ports import GraphStore

V = "https://kb.local/vocab#"


@dataclass
class Expansion:
    concepts: list[str] = field(default_factory=list)  # spotted in the question
    related: list[str] = field(default_factory=list)  # found in the caller's graphs
    terms: list[str] = field(default_factory=list)
    graphs: list[str] = field(default_factory=list)  # the dataset the SPARQL could see
    evidence: list[str] = field(default_factory=list)  # entity labels that linked concepts (audit/debug)
    labels: list[str] = field(default_factory=list)  # preferred labels of the spotted concepts (ontology only)

    @property
    def concept_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.concepts, *self.related]))

    def rerank_query(self, question: str) -> str:
        """The question plus the preferred labels of the concepts spotted in it, for the reranker.

        Without this the cross-encoder scores an expansion hit against the user's wording only
        ("wire transfer" vs a chunk saying "telegraphic transfer") and drops it below min_rerank_score.
        Only ontology labels of concepts spotted in the question itself are added: no related concepts,
        no BU graph content, so nothing outside the caller's scope can enter the query.
        """
        q = norm_label(question)
        extra = [lb for lb in self.labels if lb and lb not in q]
        return f"{question} ({'; '.join(extra)})" if extra else question


class GraphExpander:
    def __init__(self, store: GraphStore, ontology: Ontology, max_terms: int = 10, max_related: int = 3) -> None:
        self.store, self.o, self.max_terms, self.max_related = store, ontology, max_terms, max_related

    def graphs_for(self, readable_bus: Sequence[str]) -> list[str]:
        return [graph_iri(bu) for bu in sorted(set(readable_bus))] + [ONTOLOGY_GRAPH]

    def expand(self, question: str, readable_bus: Sequence[str]) -> Expansion:
        concepts = self.o.spot(question)
        exp = Expansion(
            concepts=concepts,
            graphs=self.graphs_for(readable_bus),
            labels=[norm_label(self.o.concepts[c].label) for c in concepts],
        )
        if not concepts:
            return exp
        values = " ".join(f"<{c}>" for c in concepts)
        bu_graphs = [g for g in exp.graphs if g != ONTOLOGY_GRAPH]
        related: dict[str, int] = {}
        if bu_graphs:
            rows = self.store.query(
                f"""SELECT ?c2 ?label (COUNT(*) AS ?n) WHERE {{
                      VALUES ?c1 {{ {values} }}
                      {{ ?e1 <{V}concept> ?c1 . ?e1 <{V}related>|^<{V}related> ?e2 . ?e2 <{V}concept> ?c2 .
                         ?e1 <http://www.w3.org/2000/01/rdf-schema#label> ?label }}
                      UNION
                      {{ ?ch <{V}mentions> ?c1 , ?c2 . BIND("" AS ?label) }}
                      FILTER (?c2 NOT IN ({", ".join(f"<{c}>" for c in concepts)}))
                    }} GROUP BY ?c2 ?label ORDER BY DESC(?n)""",
                bu_graphs,
            )
            for r in rows:
                if r["c2"] in self.o.concepts and r["c2"] not in self.o.spot_exclude:
                    related[r["c2"]] = related.get(r["c2"], 0) + int(r["n"])
                if r.get("label"):
                    exp.evidence.append(r["label"])
        exp.related = [c for c, _ in sorted(related.items(), key=lambda kv: -kv[1])][: self.max_related]
        terms: list[str] = []
        for c in concepts:
            terms += self.o.expansion_terms(c)
        for c in exp.related:
            terms.append(norm_label(self.o.concepts[c].label))
        q = norm_label(question)
        exp.terms = [t for t in dict.fromkeys(terms) if t and t not in q][: self.max_terms]
        exp.evidence = list(dict.fromkeys(exp.evidence))[:10]
        return exp
