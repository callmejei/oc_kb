"""Fixtures for the browser end-to-end tests (Phase 6), unique per run so runs never see each other's facts.

    uv run python -m synthetic.e2e prepare --run <id>   -> prints JSON for Playwright

Creates (synthetic, obviously fake values):
- a SharePoint folder ``retail/E2E/<run>/`` holding one policy PDF with a run-unique fact and a table,
  registered in the simulated drive ledger so a new source on that folder sees it on its first load;
- a PDF for the upload flow with a different run-unique fact.
The browser test excludes / deletes what it created at the end, so nothing stays searchable.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from kb.config import load_settings
from synthetic.content import DocContent, Section, Table
from synthetic.sharepoint import SharePointSim
from synthetic.writers import write_pdf


def _policy(run: str, rate: str) -> DocContent:
    return DocContent(
        title=f"Kestrel {run} Saver Account Terms",
        subtitle="Retail deposit product terms | Version 1.0 | synthetic E2E fixture",
        version=1,
        sections=[
            Section("1. Purpose", [f"These terms describe the Kestrel {run} Saver account (synthetic test product)."]),
            Section(
                "2. Rates",
                [f"The Kestrel {run} Saver bonus rate is {rate}% per annum, paid monthly."],
                table=Table(
                    "Table 1: Kestrel rate tiers",
                    ["Balance band", "Base rate", "Bonus rate"],
                    [["0 - 9,999", "0.05%", f"{rate}%"], ["10,000 - 49,999", "0.10%", f"{rate}%"]],
                ),
            ),
            Section("3. Eligibility", ["Individuals aged 18 and above with a retail current account."]),
        ],
    )


def _upload(run: str, fee: str) -> DocContent:
    return DocContent(
        title=f"Heron {run} Pricing Note",
        subtitle="Working note | Version 1.0 | synthetic E2E fixture",
        version=1,
        sections=[
            Section("1. Purpose", [f"Draft pricing for the Heron {run} card (synthetic test product)."]),
            Section("2. Fees", [f"The Heron {run} card annual fee is {fee} dollars, waived in the first year."]),
        ],
    )


def prepare(run: str) -> dict[str, str]:
    settings = load_settings("local")
    data = settings.path("data_dir")
    rng = random.Random(run)
    rate = f"{rng.randint(2, 4)}.{rng.randint(10, 89)}"
    fee = f"{rng.randint(101, 199)}"
    sp_root = data / "sharepoint"
    rel = f"retail/E2E/{run}/Kestrel_{run}_Saver_Terms.pdf"
    path = sp_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(_policy(run, rate), path)
    sim = SharePointSim(sp_root)
    sim.upsert(rel, seed=f"e2e:{run}")
    sim.save()
    up = data / "e2e" / run / f"Heron_{run}_Pricing_Note.pdf"
    up.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(_upload(run, fee), up)
    return {
        "run": run,
        "source_id": f"ret-e2e-{run}".lower(),
        "site": "retail",
        "folder": f"/E2E/{run}",
        "policy_title": f"Kestrel_{run}_Saver_Terms",  # SharePoint docs are titled by file name stem
        "policy_question": f"What is the bonus rate of the Kestrel {run} Saver account?",
        "policy_answer": rate,
        "upload_path": str(Path(up).resolve()),
        "upload_title": up.name,
        "upload_question": f"What is the annual fee of the Heron {run} card?",
        "upload_answer": fee,
    }


def cleanup(run: str) -> dict[str, list[str]]:
    """Backstop after a run (pass or fail): delete the run's files from the simulated drive, so the next
    delta sync removes anything still indexed; drop the local upload fixture."""
    import shutil

    data = load_settings("local").path("data_dir")
    sp_root = data / "sharepoint"
    sim = SharePointSim(sp_root)
    gone = [i.path for i in list(sim.items.values()) if i.path.startswith(f"retail/E2E/{run}/") and not i.deleted]
    for rel in gone:
        sim.delete(rel)
    sim.save()
    shutil.rmtree(sp_root / "retail" / "E2E" / run, ignore_errors=True)
    shutil.rmtree(data / "e2e" / run, ignore_errors=True)
    return {"deleted_from_drive": gone}


def main() -> None:
    ap = argparse.ArgumentParser(prog="synthetic.e2e")
    ap.add_argument("cmd", choices=["prepare", "cleanup"])
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    print(json.dumps(prepare(a.run) if a.cmd == "prepare" else cleanup(a.run)))


if __name__ == "__main__":
    main()
