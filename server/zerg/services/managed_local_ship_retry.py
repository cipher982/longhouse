"""Shared explicit wait timing for managed-local Claude transcript shipping.

The remaining managed-local hot-path tail is mostly the gap between Claude
finishing locally and the transcript becoming parseable enough for a useful
single-file ship. Keep that wait window shared across the Stop hook and the
direct managed-local ship command so the engine owns the readiness polling
instead of shell retry ladders, but preserve the old long-tail coverage so
late transcript flushes still get a useful explicit ship attempt.
"""

from __future__ import annotations

MANAGED_LOCAL_CLAUDE_SHIP_WAIT_READY_MS = 8000
