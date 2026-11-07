"""Concept -> mart table/column hints for text-to-SQL (graphify-fibo skill step 8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from kb.config import REPO_ROOT
from kb.graph.ontology import Ontology


class TableMap:
    def __init__(self, ontology: Ontology, path: str | Path = "config/table_map.yaml") -> None:
        p = Path(path)
        p = p if p.is_absolute() else REPO_ROOT / p
        raw: dict[str, dict[str, Any]] = yaml.safe_load(p.read_text(encoding="utf-8"))
        self.o = ontology
        self.entries = {ontology.resolve(k): v for k, v in raw.items()}

    def tables(self) -> set[str]:
        return {v["table"] for v in self.entries.values()}

    def hints(self, concepts: list[str], granted_tables: list[str]) -> list[str]:
        """Hints only for spotted concepts whose table the caller may query."""
        out = []
        for c in concepts:
            e = self.entries.get(c)
            if e and e["table"] in granted_tables:
                out.append(f"- '{self.o.concepts[c].label}' -> {e['table']}: {e['hint']}")
        return out
