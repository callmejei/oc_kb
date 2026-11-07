"""YamlIdentityProvider: config/users.yaml -> Identity (local stand-in for SSO + LDAP)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from kb.config import REPO_ROOT
from kb.ports.types import Identity


class UnknownUserError(LookupError):
    pass


class YamlIdentityProvider:
    STATUS = "implemented"
    PHASE = 0

    def __init__(self, path: str | Path, **_: Any) -> None:
        p = Path(path)
        self.path = p if p.is_absolute() else REPO_ROOT / p
        doc = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.groups: dict[str, dict[str, str]] = doc["groups"]
        self._users: dict[str, Identity] = {}
        for u in doc["users"]:
            unknown = set(u["groups"]) - set(self.groups)
            if unknown:
                raise ValueError(f"user {u['id']} references unknown groups {sorted(unknown)}")
            if u["id"] in self._users:
                raise ValueError(f"duplicate user id {u['id']}")
            self._users[u["id"]] = Identity(
                user_id=u["id"], display_name=u["name"], bu=u["bu"], groups=frozenset(u["groups"])
            )

    def resolve(self, user_id: str) -> Identity:
        try:
            return self._users[user_id]
        except KeyError:
            raise UnknownUserError(user_id) from None

    def all_users(self) -> list[Identity]:
        return list(self._users.values())

    def governance_roles(self, identity: Identity) -> set[tuple[str, str]]:
        """(bu, MAKER|CHECKER) pairs, synced into kb.governance_role in Phase 1."""
        out: set[tuple[str, str]] = set()
        for g in identity.groups:
            meta = self.groups[g]
            if meta["kind"] in ("maker", "checker"):
                out.add((meta["bu"], meta["kind"].upper()))
        return out

    def readable_bus(self, identity: Identity) -> set[str]:
        return {self.groups[g]["bu"] for g in identity.groups if self.groups[g]["kind"] in ("read", "maker", "checker")}
