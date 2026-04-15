"""JWT helpers shared across auth entrypoints."""

from __future__ import annotations

from typing import Any
from typing import Iterable

from zerg.auth.strategy import _decode_jwt_fallback


def decode_jwt_with_secret_candidates(token: str, secrets: Iterable[str]) -> dict[str, Any] | None:
    """Decode ``token`` with the first valid secret from ``secrets``."""

    seen: set[str] = set()
    for secret in secrets:
        key = str(secret or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            return _decode_jwt_fallback(token, key)
        except Exception:
            continue
    return None


__all__ = ["decode_jwt_with_secret_candidates"]
