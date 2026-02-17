"""CI test job — proves deps are installed and job execution works."""

from __future__ import annotations

from typing import Any

# Import from requirements.txt dep to prove deps were installed
from dateutil.parser import parse as parse_date


async def run() -> dict[str, Any]:
    """Simple job that verifies dateutil is importable and returns success."""
    # Exercise the imported dep so it's not just a dead import
    ts = parse_date("2026-01-01T00:00:00Z")
    return {
        "success": True,
        "message": "echo job executed",
        "dateutil_works": ts.year == 2026,
    }
