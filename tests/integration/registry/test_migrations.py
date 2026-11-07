"""Migration tooling and schema hygiene."""

from __future__ import annotations

from importlib import resources

import pytest

from kb.config import REPO_ROOT
from kb.registry import migrate


def test_001_is_the_reference_schema_without_transaction_wrapper():
    ref = (REPO_ROOT / "reference" / "kb_registry_schema.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in ref.split("\n") if ln.strip() not in ("BEGIN;", "COMMIT;"))
    mig = (resources.files("kb.registry") / "migrations" / "001_registry.sql").read_text(encoding="utf-8")
    assert mig.split("\n\n", 1)[1] == body


def test_all_migrations_applied_in_order(regdb, as_admin):
    with as_admin() as c:
        rows = c.execute("SELECT version FROM kb_meta.schema_migration ORDER BY version").fetchall()
    assert (
        [r["version"] for r in rows]
        == [m.version for m in migrate.discover()]
        == ["000", "001", "002", "003", "004", "005", "006"]
    )


def test_rerun_is_a_noop(regdb):
    assert migrate.migrate(regdb, log=lambda *_: None) == []


def test_edited_migration_is_refused(regdb, as_admin):
    with as_admin() as c:
        c.execute("UPDATE kb_meta.schema_migration SET checksum = repeat('0', 64) WHERE version = '002'")
    try:
        with pytest.raises(migrate.MigrationError, match="002_uploads changed"):
            migrate.migrate(regdb, log=lambda *_: None)
    finally:
        real = next(m for m in migrate.discover() if m.version == "002").checksum
        with as_admin() as c:
            c.execute("UPDATE kb_meta.schema_migration SET checksum = %s WHERE version = '002'", (real,))


def test_owner_is_not_superuser_and_owns_every_function(as_admin):
    with as_admin() as c:
        owner = c.execute(
            "SELECT pg_get_userbyid(nspowner) AS o, r.rolsuper FROM pg_namespace n "
            "JOIN pg_roles r ON r.oid = n.nspowner WHERE nspname = 'kb'"
        ).fetchone()
        owners = c.execute(
            "SELECT DISTINCT pg_get_userbyid(proowner) AS o FROM pg_proc WHERE pronamespace = 'kb'::regnamespace"
        ).fetchall()
    assert owner == {"o": "kb_owner", "rolsuper": False}
    assert owners == [{"o": "kb_owner"}]


def test_no_function_executable_by_public(as_admin):
    with as_admin() as c:
        n = c.execute(
            "SELECT count(*) AS n FROM pg_proc p WHERE pronamespace = 'kb'::regnamespace "
            "AND has_function_privilege('public', p.oid, 'EXECUTE')"
        ).fetchone()
    assert n["n"] == 0


def test_001_constraints_survive_002(as_admin):
    with as_admin() as c:
        names = {
            r["conname"]
            for r in c.execute(
                "SELECT conname FROM pg_constraint WHERE connamespace = 'kb'::regnamespace AND contype = 'c'"
            )
        }
    assert {
        "approval_request_four_eyes",
        "approval_request_decision_shape",
        "approval_request_target_shape",
        "approval_request_reject_reason",
        "approval_request_executed_shape",
        "kb_source_type_shape",
        "document_origin_shape",
    } <= names
