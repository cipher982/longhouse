"""Pure helpers for the timeline headline (the frozen per-session title).

The timeline card wants one stable, human-readable headline per session — the
"what is this about" anchor the user builds muscle memory on. Two concerns live
here, both pure (no DB, no IO) so iOS/web/widget can rely on identical output:

- ``sanitize_title``: turn arbitrary first-message / summary text into a short,
  clean phrase. Pasted prompts arrive as ``\"\"\"`` fences, ``[Image #1]``, URLs,
  and markdown noise; rendering them raw is the timeline's garbage-preview bug.
- ``resolve_timeline_title``: the fallback ladder that always yields a non-empty
  headline, preferring the frozen ``anchor_title`` so the row stays stable as the
  live ``summary_title`` keeps drifting underneath.
"""

from __future__ import annotations

import re

# Heuristic word budget for a glanceable headline. ~8 words fits an iOS row.
_MAX_TITLE_WORDS = 8
_MAX_TITLE_CHARS = 80

_SUMMARIZING_PLACEHOLDER = "Summarizing…"

# Noise we strip before extracting words. Order matters: fenced blocks and
# images go first so their contents never leak into the headline.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Any run of code-fence backticks (handles unterminated fences in mid-paste
# first messages, e.g. "```\nplease fix").
_LOOSE_FENCE_RE = re.compile(r"`{3,}[a-zA-Z0-9_-]*")
# Inline code is stripped entirely (not unwrapped): a backticked command or
# path is noise, not a headline.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")  # <thinking>, </system>, HTML tags
_IMAGE_TAG_RE = re.compile(r"\[image[^\]]*\]", re.IGNORECASE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_TRIPLE_QUOTE_RE = re.compile(r'"""|\'\'\'')
_HEADING_PREFIX_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_title(text: str | None, *, max_words: int = _MAX_TITLE_WORDS) -> str | None:
    """Reduce arbitrary text to a short clean headline phrase, or None.

    Strips code fences, image tags, markdown links, and URLs, collapses
    whitespace, then keeps the first ``max_words`` words. Returns None when
    nothing meaningful survives so callers can fall through the ladder.
    """
    if not text:
        return None

    cleaned = _CONTROL_CHARS_RE.sub(" ", text)
    cleaned = _FENCE_RE.sub(" ", cleaned)
    cleaned = _LOOSE_FENCE_RE.sub(" ", cleaned)  # unterminated/leftover fences
    cleaned = _MD_IMAGE_RE.sub(" ", cleaned)
    cleaned = _IMAGE_TAG_RE.sub(" ", cleaned)
    cleaned = _MD_LINK_RE.sub(r"\1", cleaned)  # keep link label, drop target
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)  # drop backticked code/paths
    cleaned = _TAG_RE.sub(" ", cleaned)  # <thinking>, html, tool tags
    cleaned = _TRIPLE_QUOTE_RE.sub(" ", cleaned)

    # First line with real (alphanumeric) content, heading marker stripped.
    # Skips lines that are only punctuation/quotes left over from stripping.
    line = ""
    for raw_line in cleaned.splitlines():
        candidate = _HEADING_PREFIX_RE.sub("", raw_line).strip()
        if candidate and any(ch.isalnum() for ch in candidate):
            line = candidate
            break
    if not line:
        return None

    line = _WHITESPACE_RE.sub(" ", line).strip()
    if not line:
        return None

    words = line.split(" ")
    if len(words) > max_words:
        line = " ".join(words[:max_words]).rstrip(",.;:—-") + "…"

    if len(line) > _MAX_TITLE_CHARS:
        line = line[: _MAX_TITLE_CHARS - 1].rstrip() + "…"

    return line or None


def structured_fallback_title(project: str | None, git_branch: str | None = None) -> str:
    """Last-resort headline when there is no content to summarize."""
    parts = [p for p in (project, git_branch) if p and p.strip()]
    if parts:
        return " · ".join(p.strip() for p in parts)
    return "Untitled session"


def resolve_timeline_title(
    *,
    anchor_title: str | None,
    summary_title: str | None,
    summary_status: str | None,
    first_user_message: str | None,
    project: str | None,
    git_branch: str | None = None,
) -> str:
    """Resolve the stable headline a client should render. Always non-empty.

    Ladder (highest signal + most stable first):
      1. frozen ``anchor_title`` — the muscle-memory anchor, set once
      2. a ready ``summary_title`` not yet frozen (first render before freeze)
      3. a sanitized ``first_user_message`` — real signal beats a placeholder
      4. ``"Summarizing…"`` while a summary is genuinely pending
      5. structured ``{project} · {branch}`` fallback

    Note: this prefers a usable first-message over the "Summarizing…" placeholder
    (a deliberate deviation from the spec's literal ordering) so a brand-new row
    shows real content instead of a spinner-word whenever it can.
    """
    frozen = sanitize_title(anchor_title)
    if frozen:
        return frozen

    ready = sanitize_title(summary_title)
    if ready:
        return ready

    from_message = sanitize_title(first_user_message)
    if from_message:
        return from_message

    if summary_status == "pending":
        return _SUMMARIZING_PLACEHOLDER

    return structured_fallback_title(project, git_branch)


def freeze_anchor_title(summary_title: str | None) -> str | None:
    """Sanitized snapshot to persist as the frozen anchor, or None to skip.

    Called when a summary first becomes ready (write-once) or when a session
    closes (promotion). Never freezes garbage: if the title sanitizes to
    nothing, returns None and the row stays unfrozen until a better title
    arrives (or close-time promotion fills it in).
    """
    return sanitize_title(summary_title)
