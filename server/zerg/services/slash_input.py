"""Slash commands the owner sends from the app (the server's half).

The engine decides how each provider runs a command (engine/src/slash_input.rs).
The server's only rule: a command never enters a running turn. A slash command
sent as a steer is queued instead and runs when the session is idle, as typing
it in the provider's terminal mid-turn would.
"""

from __future__ import annotations

import re

_COMMAND = re.compile(r"^\s*/(?!/)[A-Za-z0-9_\-:.]+(?:\s|$)")


def is_slash_command(text: str) -> bool:
    """``/name args`` (not ``//`` literal text, not ``/tmp/path`` text)."""

    return bool(_COMMAND.match(text or ""))
