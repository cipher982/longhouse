"""Telegram message formatting helpers."""

from __future__ import annotations

import re


def format_for_telegram(text: str) -> str:
    """Convert basic Markdown output to Telegram HTML."""
    placeholders: list[str] = []

    def _stash(match: re.Match) -> str:
        placeholders.append(match.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    text = re.sub(r"```(?:[\w+-]*)?\n?(.*?)```", _stash, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", _stash, text)

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"\*([^*\n]+)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    def _restore(match: re.Match) -> str:
        idx = int(match.group(1))
        raw = placeholders[idx]
        if raw.startswith("```"):
            inner = re.sub(r"^```(?:[\w+-]*)?\n?", "", raw)
            if inner.endswith("```"):
                inner = inner[:-3]
            inner = inner.rstrip("\n")
        else:
            inner = raw[1:-1] if raw.startswith("`") and raw.endswith("`") else raw

        inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        tag = "pre" if raw.startswith("```") else "code"
        return f"<{tag}>{inner}</{tag}>"

    return re.sub(r"\x00(\d+)\x00", _restore, text)
