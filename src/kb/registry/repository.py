"""Registry repository: thin SQL over the governed schema.

Every function takes a connection opened by ``PostgresRegistry.transaction(actor)``, so the
database knows who is acting. Authorization lives in the database (constraints, triggers,
SECURITY DEFINER functions); this module never re-implements it, it only shapes data.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

Conn = Connection[dict[str, Any]]

REVISION_FIELDS: tuple[str, ...] = (
    "classification",
    "retention_class",
    "acl_groups",
    "allowed_extensions",
    "max_file_bytes",
    "include_paths",
    "exclude_paths",
    "pii_policy",
)
SOURCE_EDITABLE: tuple[str, ...] = ("display_name", "sp_folder_path")


def _revision_values(fields: dict[str, Any]) -> dict[str, Any]:
    out = {k: fields[k] for k in REVISION_FIELDS if k in fields}
    if "pii_policy" in out:
        out["pii_policy"] = Jsonb(out["pii_policy"])
    return out


# ---------------------------------------------------------------------------- sources / revisions
def create_source(conn: Conn, source: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    conn.execute(
        """INSERT INTO kb.kb_source (source_id, bu, display_name, source_type, sp_site_id, sp_drive_id,
                                     sp_folder_path, created_by)
           VALUES (%(source_id)s, %(bu)s, %(display_name)s, %(source_type)s, %(sp_site_id)s, %(sp_drive_id)s,
                   %(sp_folder_path)s, kb.current_actor())""",
        {"sp_site_id": None, "sp_drive_id": None, "sp_folder_path": None, **source},
    )
    rev = insert_revision(conn, source["source_id"], 1, revision)
    return {"source": get_source(conn, source["source_id"]), "revision": rev}


def insert_revision(conn: Conn, source_id: str, revision_no: int, fields: dict[str, Any]) -> dict[str, Any]:
    values = _revision_values(fields)
    cols = [sql.Identifier(c) for c in values]
    q = sql.SQL(
        "INSERT INTO kb.source_revision (source_id, revision_no, created_by, {cols}) "
        "VALUES (%(source_id)s, %(revision_no)s, kb.current_actor(), {vals}) RETURNING *"
    ).format(cols=sql.SQL(", ").join(cols), vals=sql.SQL(", ").join(sql.Placeholder(c) for c in values))
    row = conn.execute(q, {"source_id": source_id, "revision_no": revision_no, **values}).fetchone()
    assert row is not None
    return row


def create_revision(conn: Conn, source_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """New DRAFT revision based on the current approved revision (or the latest one)."""
    open_draft = conn.execute(
        "SELECT revision_id FROM kb.source_revision WHERE source_id = %s AND status IN ('DRAFT','SUBMITTED')",
        (source_id,),
    ).fetchone()
    if open_draft:
        raise ConflictError(f"source {source_id} already has an open revision {open_draft['revision_id']}")
    base = conn.execute(
        """SELECT r.* FROM kb.source_revision r
           LEFT JOIN kb.kb_source s ON s.current_revision_id = r.revision_id
           WHERE r.source_id = %s
           ORDER BY (s.source_id IS NOT NULL) DESC, r.revision_no DESC LIMIT 1""",
        (source_id,),
    ).fetchone()
    if base is None:
        raise NotFoundError(f"unknown source {source_id}")
    next_no = conn.execute(
        "SELECT max(revision_no) + 1 AS n FROM kb.source_revision WHERE source_id = %s", (source_id,)
    ).fetchone()
    assert next_no is not None
    fields = {k: base[k] for k in REVISION_FIELDS} | changes
    return insert_revision(conn, source_id, next_no["n"], fields)


def update_revision(conn: Conn, revision_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    values = _revision_values(changes)
    if not values:
        return get_revision(conn, revision_id)
    q = sql.SQL("UPDATE kb.source_revision SET {sets} WHERE revision_id = %(rid)s RETURNING *").format(
        sets=sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in values)
    )
    row = conn.execute(q, {"rid": revision_id, **values}).fetchone()
    if row is None:
        raise NotFoundError(f"unknown revision {revision_id}")
    return row


def get_source(conn: Conn, source_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM kb.kb_source WHERE source_id = %s", (source_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"unknown source {source_id}")
    return row


def get_revision(conn: Conn, revision_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM kb.source_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"unknown revision {revision_id}")
    return row


def list_sources(conn: Conn, bus: list[str]) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT s.*, r.revision_no AS current_revision_no, r.acl_groups AS current_acl_groups,
                  (SELECT request_id FROM kb.approval_request a
                    WHERE a.source_id = s.source_id AND a.status = 'PENDING'
                      AND a.request_type IN ('ONBOARD_SOURCE','CHANGE_SOURCE','REACTIVATE_SOURCE'))
                  AS pending_request_id
           FROM kb.kb_source s LEFT JOIN kb.source_revision r ON r.revision_id = s.current_revision_id
           WHERE s.bu = ANY(%s) ORDER BY s.source_id""",
        (bus,),
    ).fetchall()


# ---------------------------------------------------------------------------- requests
def submit_revision(conn: Conn, revision_id: str, comment: str | None) -> str:
    rev = get_revision(conn, revision_id)
    src = get_source(conn, rev["source_id"])
    rtype = "CHANGE_SOURCE" if src["current_revision_id"] else "ONBOARD_SOURCE"
    row = conn.execute(
        "SELECT kb.submit_request(%s, %s, %s, NULL, NULL, %s) AS id",
        (rtype, rev["source_id"], revision_id, comment),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def withdraw(conn: Conn, request_id: str, reason: str | None) -> None:
    _ensure_request(conn, request_id)
    conn.execute("SELECT kb.withdraw_request(%s, %s)", (request_id, reason))


def decide(conn: Conn, request_id: str, approve: bool, reason: str | None) -> None:
    conn.execute("SELECT kb.decide_request(%s, %s, %s)", (request_id, approve, reason))


def request_publish(conn: Conn, doc_id: str, comment: str | None) -> str:
    row = conn.execute("SELECT kb.request_publish(%s, %s) AS id", (doc_id, comment)).fetchone()
    assert row is not None
    return str(row["id"])


def unpublish(conn: Conn, doc_id: str, reason: str) -> str:
    row = conn.execute("SELECT kb.unpublish_document(%s, %s) AS id", (doc_id, reason)).fetchone()
    assert row is not None
    return str(row["id"])


def _ensure_request(conn: Conn, request_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM kb.approval_request WHERE request_id = %s", (request_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"unknown request {request_id}")
    return row


def inbox(conn: Conn, actor: str, checker_bus: list[str]) -> list[dict[str, Any]]:
    """Pending requests a checker may decide: their BUs, never their own."""
    return conn.execute(
        """SELECT a.*, s.bu, s.display_name AS source_name, s.source_type
           FROM kb.approval_request a JOIN kb.kb_source s USING (source_id)
           WHERE a.status = 'PENDING' AND s.bu = ANY(%s) AND a.maker_id <> %s
           ORDER BY a.submitted_at""",
        (checker_bus, actor),
    ).fetchall()


def request_detail(conn: Conn, request_id: str) -> dict[str, Any]:
    """Request plus what a checker needs: proposed vs current config (and the document for publishes)."""
    req = _ensure_request(conn, request_id)
    src = get_source(conn, req["source_id"])
    out: dict[str, Any] = {"request": req, "source": src}
    if req["revision_id"]:
        proposed = get_revision(conn, str(req["revision_id"]))
        current = get_revision(conn, str(src["current_revision_id"])) if src["current_revision_id"] else None
        out |= {"proposed": proposed, "current": current, "diff": revision_diff(current, proposed)}
    if req["doc_id"]:
        out["document"] = conn.execute(
            "SELECT doc_id, current_path, title, origin, uploaded_by, visibility, status FROM kb.document "
            "WHERE doc_id = %s",
            (req["doc_id"],),
        ).fetchone()
        cur = get_revision(conn, str(src["current_revision_id"]))["acl_groups"] if src["current_revision_id"] else []
        uploader = out["document"]["uploaded_by"] if out["document"] else None
        out["acl_change"] = {"from": [f"user:{uploader}"], "to": list(cur)}
    return out


def revision_diff(current: dict[str, Any] | None, proposed: dict[str, Any]) -> dict[str, Any]:
    changes = []
    for f in REVISION_FIELDS:
        old = current[f] if current else None
        if old != proposed[f]:
            changes.append({"field": f, "current": old, "proposed": proposed[f]})
    cur_acl = set(current["acl_groups"]) if current else set()
    new_acl = set(proposed["acl_groups"])
    return {
        "changes": changes,
        "acl_added": sorted(new_acl - cur_acl),
        "acl_removed": sorted(cur_acl - new_acl),
    }


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass
