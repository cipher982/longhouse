from __future__ import annotations

import re

# Only these source values are Longhouse-owned; other MCP channel bodies are user text.
# Add a source only when the Longhouse channel protocol explicitly changes.
_CHANNEL_WRAPPER_RE = re.compile(
    r"""^<channel\b(?=[^>]*\ssource=(?:"longhouse(?:-channel)?"|'longhouse(?:-channel)?'))[^>]*>\n?([\s\S]*?)\n?</channel>$"""
)


def strip_claude_channel_wrapper(text: str | None) -> str:
    raw = str(text or "")
    match = _CHANNEL_WRAPPER_RE.match(raw.strip())
    if match is None:
        return raw
    return match.group(1).strip()
