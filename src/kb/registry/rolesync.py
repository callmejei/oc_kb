"""Sync MAKER/CHECKER governance roles from the IdentityProvider into kb.governance_role.

Runs as the admin under SET LOCAL ROLE kb_owner, acting as ``svc:rolesync``; every insert and
delete is audited by the table's governance trigger.

Usage: python -m kb.registry.rolesync [--database NAME]
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import psycopg

from kb.adapters.local.identity import YamlIdentityProvider
from kb.registry.migrate import OWNER_ROLE, admin_conninfo

ACTOR = "svc:rolesync"


def desired_roles(idp: YamlIdentityProvider) -> set[tuple[str, str, str]]:
    return {(u.user_id, bu, role) for u in idp.all_users() for bu, role in idp.governance_roles(u)}


def sync(conninfo: dict[str, Any], idp: YamlIdentityProvider) -> tuple[int, int]:
    want = desired_roles(idp)
    with psycopg.connect(**conninfo) as conn, conn.transaction():
        conn.execute(f"SET LOCAL ROLE {OWNER_ROLE}")
        conn.execute("SELECT set_config('kb.actor_id', %s, true)", (ACTOR,))
        have = {tuple(r) for r in conn.execute("SELECT actor_id, bu, role FROM kb.governance_role")}
        for actor, bu, role in sorted(have - want):
            conn.execute(
                "DELETE FROM kb.governance_role WHERE actor_id = %s AND bu = %s AND role = %s", (actor, bu, role)
            )
        for actor, bu, role in sorted(want - have):
            conn.execute("INSERT INTO kb.governance_role (actor_id, bu, role) VALUES (%s, %s, %s)", (actor, bu, role))
    return len(want - have), len(have - want)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb.registry.rolesync")
    ap.add_argument("--database")
    a = ap.parse_args(argv)
    added, removed = sync(admin_conninfo(a.database), YamlIdentityProvider("config/users.yaml"))
    print(f"governance roles synced: +{added} -{removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
