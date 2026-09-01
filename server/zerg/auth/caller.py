"""Authenticated owner identity passed across router boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Caller:
    """One authenticated principal with a required tenant owner.

    ``principal`` retains credential-specific facts such as device or managed
    session scope. User-data services receive ``owner_id`` from this object;
    they never infer an owner from an optional credential themselves.
    """

    owner_id: int
    principal: Any | None = None

    def __getattr__(self, name: str) -> Any:
        principal = object.__getattribute__(self, "principal")
        if principal is None:
            raise AttributeError(name)
        return getattr(principal, name)


def caller_principal(value: Any) -> Any:
    """Unwrap a router caller while accepting raw principals in direct tests."""

    return value.principal if isinstance(value, Caller) else value


__all__ = ["Caller", "caller_principal"]
