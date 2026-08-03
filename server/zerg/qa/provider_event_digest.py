"""Dependency-free digest for one provider-native JSON event."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def raw_event_digest(event: Mapping[str, Any]) -> str:
    """Digest the parsed provider row used by live provenance gates.

    Keep this small module free of server/runtime imports. Qualification
    dispatch must remain importable in the dependency-light ``python -S``
    smoke test even though the full interaction oracle uses optional runtime
    packages.
    """

    encoded = json.dumps(
        dict(event),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
