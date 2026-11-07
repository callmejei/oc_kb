"""Explicit stubs: a stub satisfies its Protocol structurally but every call raises.

README status tables read ``STATUS``/``PHASE`` so nothing is reported as implemented by accident.
"""

from __future__ import annotations

from typing import Any, NoReturn


class NotImplementedYetError(NotImplementedError):
    pass


def make_stub(name: str, protocol: type, phase: int) -> type:
    members = [m for m in dir(protocol) if not m.startswith("_")]

    def _raiser(member: str) -> Any:
        def method(self: Any, *args: Any, **kwargs: Any) -> NoReturn:
            raise NotImplementedYetError(f"{name}.{member} is stubbed; implemented in Phase {phase}")

        method.__name__ = member
        return method

    ns: dict[str, Any] = {m: _raiser(m) for m in members}
    if "dims" in members:  # property on Embedder
        ns["dims"] = property(lambda self: int(self.options.get("dims", 0)))

    def __init__(self: Any, **options: Any) -> None:  # noqa: N807
        self.options = options

    ns["__init__"] = __init__
    ns["STATUS"] = "stubbed"
    ns["PHASE"] = phase
    return type(name, (), ns)
