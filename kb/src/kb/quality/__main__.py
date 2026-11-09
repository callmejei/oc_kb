"""`kb.ps1 dq`: run the data-quality rules once as the pipeline and record the counts (same body as dq_job)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from kb.config import load_settings
from kb.quality.rules import run_and_record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.quality")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args(argv)
    out = run_and_record(load_settings(strict=True), a.as_of, record=not a.no_record)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
