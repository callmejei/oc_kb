"""Synthetic world CLI.

python -m synthetic seed                         rebuild corpus, upload fixtures, marts, manifest
python -m synthetic scenario <name> --doc KEY    new_version | delete | move_out_of_scope
python -m synthetic scenario <name> --bu BU      add_oversized | add_unsupported | add_corrupt
python -m synthetic list                         list document keys
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from synthetic.content import build_content
from synthetic.marts import build_marts
from synthetic.scenarios import LIFECYCLE, build_upload_fixtures
from synthetic.sharepoint import SharePointSim, sha256_file
from synthetic.world import CANARIES, CATALOGUE, UPLOAD_SPACES
from synthetic.writers import write

REPO_ROOT = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    from kb.config import load_settings

    return load_settings().path("data_dir")


def seed(data: Path) -> dict[str, Any]:
    sp_root = data / "sharepoint"
    if sp_root.exists():
        shutil.rmtree(sp_root)
    sim = SharePointSim(sp_root)
    docs = []
    for spec in CATALOGUE:
        path = sp_root / spec.rel_path
        write(spec.fmt, build_content(spec, 1), path, seed=f"{spec.key}:v1")
        item = sim.upsert(spec.rel_path, seed=spec.key)
        docs.append(
            {
                "key": spec.key,
                "bu": spec.bu,
                "title": spec.title,
                "kind": spec.kind,
                "fmt": spec.fmt,
                "path": f"sharepoint/{spec.rel_path}",
                "item_id": item.item_id,
                "sha256": sha256_file(path),
                "size_bytes": item.size_bytes,
                "canaries": list(spec.canaries),
                "scanned": spec.fmt == "pdfscan",
            }
        )
    sim.save()
    uploads = build_upload_fixtures(data / "upload_fixtures", sp_root)
    marts = build_marts(data / "marts" / "marts.duckdb")
    manifest = {
        "generator": "synthetic v1",
        "documents": docs,
        "canaries": [c.__dict__ for c in CANARIES],
        "upload_spaces": UPLOAD_SPACES,
        "upload_scenarios": uploads,
        "marts": marts,
    }
    (data / "synthetic_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="synthetic")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    sub.add_parser("list")
    sc = sub.add_parser("scenario")
    sc.add_argument("name", choices=sorted(LIFECYCLE))
    sc.add_argument("--doc")
    sc.add_argument("--bu")
    a = ap.parse_args(argv)
    data = data_dir()
    if a.cmd == "seed":
        m = seed(data)
        by_fmt: dict[str, int] = {}
        for d in m["documents"]:
            by_fmt[d["fmt"]] = by_fmt.get(d["fmt"], 0) + 1
        print(f"documents: {len(m['documents'])} {by_fmt}")
        print(f"canaries: {len(m['canaries'])}; upload scenarios: {len(m['upload_scenarios'])}")
        print(f"marts: {m['marts']}")
    elif a.cmd == "list":
        for spec in CATALOGUE:
            print(f"{spec.key:<28} {spec.bu:<10} {spec.fmt:<8} {spec.title}")
    else:
        fn = LIFECYCLE[a.name]
        arg = a.doc if a.name in ("new_version", "delete", "move_out_of_scope") else a.bu
        if not arg:
            ap.error("--doc or --bu required for this scenario")
        print(json.dumps(fn(data / "sharepoint", arg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
