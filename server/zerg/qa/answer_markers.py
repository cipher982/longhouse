"""Did the model answer with a marker, or only mention it?

Factory proofs tell a model to "reply with exactly <MARKER>" and judge the turn
by which marker it answered with. A substring match cannot tell the answer from
an explanation: on 2026-10-10 Haiku 5.5 obeyed a steer, answered
``LONGHOUSE_CLAUDE_STEERED_<id>`` on its own line, then added "so I did not
include `LONGHOUSE_CLAUDE_UNSTEERED_<id>`" (and, in the next run, the same
sentence without backticks). The substring check read that quote as the
original task finishing and failed a steer that worked.

A marker is *answered* when it stands alone on a line of the reply. Inline
markdown wrapping (backticks, emphasis, quotes) and trailing punctuation are
allowed; any other word on the line makes it a mention, and so does a list
item or blockquote, because a model lists or quotes markers while explaining
what it did or did not reach. Verdicts that fail a turn because the model said
a marker it was told not to reach use this; checks that a marker was produced
at all can keep a substring match, since each is paired with independent
evidence (the commands that ran, the tool that completed, the turn's stop
reason).
"""

from __future__ import annotations

_WRAPPING = "`*\"' \t“”‘’"
_TRAILING = "`*\"' \t“”‘’.!,;:"
_LIST_OR_QUOTE = ("- ", "+ ", "* ", "> ")


def marker_answered(text: str, marker: str) -> bool:
    """Return whether ``marker`` stands alone on a line of ``text``."""

    if not marker or marker not in text:
        return False
    for line in text.splitlines():
        if line.lstrip().startswith(_LIST_OR_QUOTE):
            continue
        if line.strip(_WRAPPING).rstrip(_TRAILING).strip(_WRAPPING) == marker:
            return True
    return False


def any_marker_answered(texts: list[str], marker: str) -> bool:
    return any(marker_answered(text, marker) for text in texts)
