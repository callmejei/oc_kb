"""The mandatory retrieval filter. This is the ONLY place production code builds an AccessFilter
(a test greps for that). Groups come from the live IdentityProvider on every request."""

from __future__ import annotations

from kb.ports.types import AccessFilter, Identity


def build_access_filter(identity: Identity) -> AccessFilter:
    """acl_groups must intersect the caller's AD groups + their private principal; only current versions
    of active sources. Never optional, never widened."""
    return AccessFilter(
        acl_principals=frozenset(identity.acl_principals),
        require_current=True,
        require_source_active=True,
    )


def describe(flt: AccessFilter) -> dict[str, object]:
    """Audit form of the filter actually applied."""
    return {
        "acl_principals": sorted(flt.acl_principals),
        "is_current": flt.require_current,
        "source_active": flt.require_source_active,
        "bus": sorted(flt.bus) if flt.bus is not None else None,
    }


def graph_scope(identity: Identity, readable_bus: set[str]) -> list[str]:
    """BUs whose knowledge graphs the caller may read for expansion: exactly the BUs they can read
    documents of (from the live IdentityProvider), never more."""
    return sorted(readable_bus)
