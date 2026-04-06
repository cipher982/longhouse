from __future__ import annotations

import re

_CHANNEL_WRAPPER_RE = re.compile(r"^<channel\b[^>]*>\n?([\s\S]*?)\n?</channel>$")


def strip_claude_channel_wrapper(text: str | None) -> str:
    raw = str(text or "")
    match = _CHANNEL_WRAPPER_RE.match(raw.strip())
    if match is None:
        return raw
    return match.group(1).strip()
