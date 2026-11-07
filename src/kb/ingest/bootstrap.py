"""Onboard the synthetic world into the local registry through the normal four-eyes flow.

Each BU's synthetic maker registers + submits, its synthetic checker approves; the running Dagster
daemon then dry-runs and first-loads the sources. Idempotent: existing sources are left alone.
Local synthetic data only (CLAUDE.md rule 1).

Usage: python -m kb.ingest.bootstrap [--wait]
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from kb.adapters import build
from kb.config import load_settings
from kb.registry import repository as repo

BUS = ("retail", "corporate", "wealth", "shared")
MAKER = {"retail": "ret.maker", "corporate": "corp.maker", "wealth": "wlth.maker", "shared": "shr.maker"}
CHECKER = {"retail": "ret.checker", "corporate": "corp.checker", "wealth": "wlth.checker", "shared": "shr.checker"}
LEGACY = {"retail-policies": "Phase 1 smoke-test source with a site id that is not in the synthetic tenant"}


def _revision(bu: str, max_bytes: int) -> dict[str, Any]:
    return {
        "classification": "INTERNAL",
        "retention_class": "R7Y",
        "acl_groups": [f"GRP_KB_{bu.upper()}_READ"],
        "allowed_extensions": ["pdf", "docx", "pptx", "xlsx"],
        "max_file_bytes": max_bytes,
        "include_paths": [],
        "exclude_paths": [],
        "pii_policy": {},
    }


def desired_sources() -> list[dict[str, Any]]:
    out = []
    for bu in BUS:
        out.append(
            {
                "source_id": f"{bu}-kb",
                "bu": bu,
                "display_name": f"{bu.title()} knowledge library",
                "source_type": "SHAREPOINT",
                "sp_site_id": bu,
                "sp_drive_id": f"drive-{bu}",
                "sp_folder_path": "/KB",
                "revision": _revision(bu, 25 * 1024 * 1024),
            }
        )
        if bu != "shared":
            out.append(
                {
                    "source_id": f"{bu}-uploads",
                    "bu": bu,
                    "display_name": f"{bu.title()} upload space",
                    "source_type": "UPLOAD",
                    "revision": _revision(bu, 20 * 1024 * 1024),
                }
            )
    return out


def onboard(registry: Any, log: Any = print) -> list[str]:
    created = []
    for s in desired_sources():
        bu = s["bu"]
        with registry.transaction(MAKER[bu]) as c:
            exists = c.execute("SELECT status FROM kb.kb_source WHERE source_id = %s", (s["source_id"],)).fetchone()
        if exists:
            log(f"{s['source_id']:<18} exists ({exists['status']})")
            continue
        src = {k: v for k, v in s.items() if k != "revision"}
        with registry.transaction(MAKER[bu]) as c:
            out = repo.create_source(c, src, s["revision"])
            req = repo.submit_revision(c, str(out["revision"]["revision_id"]), "synthetic world bootstrap")
        with registry.transaction(CHECKER[bu]) as c:
            repo.decide(c, req, True, "synthetic world bootstrap")
        created.append(s["source_id"])
        log(f"{s['source_id']:<18} onboarded (maker {MAKER[bu]}, checker {CHECKER[bu]})")
    for sid, why in LEGACY.items():
        with registry.transaction(CHECKER["retail"]) as c:
            row = c.execute("SELECT status FROM kb.kb_source WHERE source_id = %s", (sid,)).fetchone()
            if row and row["status"] == "ACTIVE":
                c.execute("SELECT kb.suspend_source(%s, %s)", (sid, why))
                log(f"{sid:<18} suspended: {why}")
    return created


def wait_for_first_loads(registry: Any, timeout_s: int = 1800, log: Any = print) -> bool:
    want = {s["source_id"] for s in desired_sources() if s["source_type"] == "SHAREPOINT"}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with registry.transaction("svc:bootstrap") as c:
            done = {
                r["source_id"]
                for r in c.execute(
                    "SELECT source_id FROM kb.sync_state WHERE source_id = ANY(%s) AND last_success_at IS NOT NULL",
                    (sorted(want),),
                )
            }
            failed = c.execute(
                "SELECT source_id, stats->>'error' AS err FROM kb.ingestion_run WHERE source_id = ANY(%s) "
                "AND status = 'FAILED' AND created_at > now() - interval '1 hour'",
                (sorted(want),),
            ).fetchall()
        if failed:
            log(f"failed runs: {failed}")
            return False
        if done >= want:
            log(f"first loads done: {sorted(done)}")
            return True
        log(f"waiting for first loads: {sorted(want - done)}")
        time.sleep(15)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.ingest.bootstrap")
    ap.add_argument("--wait", action="store_true", help="wait until the daemon finished every first load")
    a = ap.parse_args(argv)
    registry = build(load_settings(strict=True), "registry")
    onboard(registry)
    ok = wait_for_first_loads(registry) if a.wait else True
    registry.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
