"""Lifecycle scenarios (mutate the simulated SharePoint tenant) and upload scenario fixtures."""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path
from typing import Any

from synthetic.content import DocContent, Section, build_content
from synthetic.sharepoint import SharePointSim, sha256_file
from synthetic.world import (
    DOC_BY_KEY,
    IN_SCOPE_FOLDER,
    OUT_OF_SCOPE_FOLDER,
    SOURCE_MAX_FILE_BYTES,
    DocSpec,
)
from synthetic.writers import ZIP_DT, write, write_docx, write_pdf

# ============================================================================ lifecycle


def _spec_at(sim: SharePointSim, key: str) -> tuple[DocSpec, str]:
    spec = DOC_BY_KEY[key]
    item = sim.items.get(sim.item_id_for(spec.key))
    if item is None or item.deleted:
        raise LookupError(f"{key} is not live in the tenant")
    return spec, item.path


def new_version(sp_root: Path, key: str) -> dict[str, Any]:
    """Edit a document: content changes (LTV values etc.), same item id, version + 1."""
    sim = SharePointSim(sp_root)
    spec, rel = _spec_at(sim, key)
    next_v = sim.items[sim.item_id_for(spec.key)].version + 1
    write(spec.fmt, build_content(spec, next_v), sp_root / rel, seed=f"{spec.key}:v{next_v}")
    item = sim.upsert(rel)
    sim.save()
    return {"scenario": "new_version", "doc": key, "item_id": item.item_id, "version": item.version}


def delete(sp_root: Path, key: str) -> dict[str, Any]:
    sim = SharePointSim(sp_root)
    _, rel = _spec_at(sim, key)
    item = sim.delete(rel)
    sim.save()
    return {"scenario": "delete", "doc": key, "item_id": item.item_id}


def move_out_of_scope(sp_root: Path, key: str) -> dict[str, Any]:
    sim = SharePointSim(sp_root)
    spec, rel = _spec_at(sim, key)
    new_rel = f"{spec.bu}/{OUT_OF_SCOPE_FOLDER}/{spec.filename}"
    item = sim.move(rel, new_rel)
    sim.save()
    return {"scenario": "move_out_of_scope", "doc": key, "item_id": item.item_id, "to": new_rel}


def _register(sp_root: Path, rel: str, data_writer: Any, scenario: str) -> dict[str, Any]:
    sim = SharePointSim(sp_root)
    path = sp_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data_writer(path)
    item = sim.upsert(rel, seed=f"scenario:{rel}")
    sim.save()
    return {"scenario": scenario, "path": rel, "item_id": item.item_id, "size_bytes": item.size_bytes}


def add_oversized(sp_root: Path, bu: str) -> dict[str, Any]:
    """A valid-looking PDF just over the source size limit."""

    def w(p: Path) -> None:
        with open(p, "wb") as f:
            f.write(b"%PDF-1.4\n% synthetic oversized file\n")
            f.write(b"0" * (SOURCE_MAX_FILE_BYTES + 1024 * 1024))
            f.write(b"\n%%EOF\n")

    return _register(sp_root, f"{bu}/{IN_SCOPE_FOLDER}/Policies/Oversized_Archive_Scan.pdf", w, "add_oversized")


def add_unsupported(sp_root: Path, bu: str) -> dict[str, Any]:
    """An MS Project file (OLE compound document magic): not an allowed type."""

    def w(p: Path) -> None:
        p.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 4096)

    return _register(sp_root, f"{bu}/{IN_SCOPE_FOLDER}/Procedures/Project_Plan.mpp", w, "add_unsupported")


def add_corrupt(sp_root: Path, bu: str) -> dict[str, Any]:
    """Allowed extension and PDF magic, but truncated garbage: must fail parsing."""

    def w(p: Path) -> None:
        p.write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >>\n" + bytes(range(256)) * 8)

    return _register(sp_root, f"{bu}/{IN_SCOPE_FOLDER}/Products/Corrupt_Product_Sheet.pdf", w, "add_corrupt")


LIFECYCLE = {
    "new_version": new_version,
    "delete": delete,
    "move_out_of_scope": move_out_of_scope,
    "add_oversized": add_oversized,
    "add_unsupported": add_unsupported,
    "add_corrupt": add_corrupt,
}

# ============================================================================ uploads

PRIVATE_SECRET = "TEALSPARROW"


def _private_note(version: int) -> DocContent:
    return DocContent(
        title="My Notes - Mortgage FAQ Draft",
        subtitle=f"Personal working notes | Version {version}.0 | synthetic",
        version=version,
        sections=[
            Section("1. Purpose", [f"Draft answers to common mortgage questions (version {version})."]),
            Section(
                "2. Notes",
                [
                    f"Private upload canary: the draft savings campaign codename is {PRIVATE_SECRET}.",
                    "Customers often ask how the loan-to-value limit changes for a second property.",
                    *(["Version 2 adds guidance on refinancing timelines."] if version > 1 else []),
                ],
            ),
        ],
    )


def build_upload_fixtures(root: Path, sp_root: Path) -> list[dict[str, Any]]:
    """Write upload fixture files and return their scenario descriptors (for the manifest)."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    out: list[dict[str, Any]] = []

    def rec(sid: str, path: Path, **kw: Any) -> None:
        out.append(
            {
                "scenario": sid,
                "file": path.relative_to(root.parent).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                **kw,
            }
        )

    p = root / "private_upload" / "My_Notes_Mortgage_FAQ.docx"
    p.parent.mkdir(parents=True)
    write_docx(_private_note(1), p)
    rec(
        "private_upload",
        p,
        user="ret.user1",
        space="retail-uploads",
        action="upload_private",
        expect="visible only to ret.user1; not to ret.user2 or other retail users",
        secret=PRIVATE_SECRET,
    )

    p2 = root / "new_version" / "My_Notes_Mortgage_FAQ.docx"
    p2.parent.mkdir(parents=True)
    write_docx(_private_note(2), p2)
    rec(
        "reupload_new_version",
        p2,
        user="ret.user1",
        space="retail-uploads",
        action="upload_new_version",
        of_scenario="private_upload",
        expect="same doc_id, version 2 current, version 1 hidden",
    )

    p3 = root / "publish_approved" / "Branch_Queue_Management_Tips.pdf"
    p3.parent.mkdir(parents=True)
    write_pdf(
        DocContent(
            "Branch Queue Management Tips",
            "Retail branch tips | synthetic",
            1,
            [
                Section(
                    "1. Tips",
                    [
                        "Open a second counter when more than eight customers wait.",
                        "Retail upload fact: the queue alert colour is MAGENTA-7.",
                    ],
                )
            ],
        ),
        p3,
    )
    rec(
        "publish_approved",
        p3,
        user="ret.user1",
        space="retail-uploads",
        action="upload_and_publish",
        checker="ret.checker",
        decision="approve",
        expect="private until approved; then visible to retail readers",
        secret="MAGENTA-7",
    )

    p4 = root / "publish_rejected" / "Deal_Memo_Draft.docx"
    p4.parent.mkdir(parents=True)
    write_docx(
        DocContent(
            "Deal Memo Draft",
            "Corporate working draft | synthetic",
            1,
            [Section("1. Memo", ["Corporate upload fact: the draft memo reference is DM-0099-RAVEN."])],
        ),
        p4,
    )
    rec(
        "publish_rejected",
        p4,
        user="corp.user1",
        space="corporate-uploads",
        action="upload_and_publish",
        checker="corp.checker",
        decision="reject",
        expect="stays private to corp.user1 after rejection",
        secret="RAVEN",
    )

    sp_doc = DOC_BY_KEY["wlth-structured-sheet"]
    p5 = root / "duplicate_of_sharepoint" / sp_doc.filename
    p5.parent.mkdir(parents=True)
    shutil.copyfile(sp_root / sp_doc.rel_path, p5)
    rec(
        "duplicate_of_sharepoint",
        p5,
        user="wlth.user1",
        space="wealth-uploads",
        action="upload_private",
        duplicate_of=sp_doc.key,
        expect="linked to the SharePoint document by SHA-256; not indexed twice",
    )

    p6 = root / "lying_extension" / "Quarterly_Report.pdf"
    p6.parent.mkdir(parents=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("report.txt", date_time=ZIP_DT), "not really a pdf\n")
    p6.write_bytes(buf.getvalue())
    rec(
        "lying_extension",
        p6,
        user="corp.user2",
        space="corporate-uploads",
        action="upload_private",
        expect="rejected at the type gate: extension .pdf but magic bytes are ZIP",
    )

    p7 = root / "forbidden_space" / "Retail_Idea.docx"
    p7.parent.mkdir(parents=True)
    write_docx(DocContent("Retail Idea", "synthetic", 1, [Section("1. Idea", ["A harmless idea."])]), p7)
    rec(
        "forbidden_space",
        p7,
        user="ret.user2",
        space="wealth-uploads",
        action="upload_private",
        expect="refused: ret.user2 has no access to the wealth upload space; nothing stored",
    )
    return out
