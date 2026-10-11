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
what it did or did not reach.

This is the one definition of "the model answered with this marker". Every
producer that waits for a marker reply or counts marker replies uses it, as
does every verdict that fails a turn because the model said a marker it was
told not to reach. A substring match there counts a turn's narration ("both
jobs must finish before I may reply with `<MARKER>`", written ahead of a tool
call) as a second reply, and an exactly-once count then never converges on a
turn that answered correctly.

Some prompts ask for a recalled value and the marker in one reply ("reply with
that value followed by exactly <MARKER>"). Models answer those on one line,
with a space between the two, on two lines, or with nothing between them at
all. ``alongside`` names the other markers such a reply is expected to carry: a
line then answers when nothing is left on it but the marker, those markers,
wrapping and punctuation. A line that carries any other word is still a
mention.

A substring match stays right where the question is not what the model
answered: a user row that carries the prompt, a tool's output, which transcript
file holds a turn, or a verdict that only needs the reply to exist and pairs it
with independent evidence (the commands that ran, the tool that completed, the
turn's stop reason).
"""

from __future__ import annotations

from collections.abc import Iterable

_WRAPPING = "`*\"' \t“”‘’"
_TRAILING = "`*\"' \t“”‘’.!,;:"
_LIST_OR_QUOTE = ("- ", "+ ", "* ", "> ")


def marker_answer_count(text: str, marker: str, *, alongside: Iterable[str] = ()) -> int:
    """Return how many times ``text`` answers with ``marker``.

    Each occurrence on an answering line counts, so a reply that repeats the
    marker on one line and a reply that gives it on two lines both count two.
    """

    if not marker or marker not in text:
        return 0
    # Longest first, so a token that contains another is taken whole.
    tokens = tuple(sorted({marker, *(token for token in alongside if token)}, key=len, reverse=True))
    answers = 0
    for line in text.splitlines():
        if marker not in line or line.lstrip().startswith(_LIST_OR_QUOTE):
            continue
        body = line.strip(_WRAPPING).rstrip(_TRAILING).strip(_WRAPPING)
        if not body.startswith(tokens) or not body.endswith(tokens):
            continue
        occurrences = 0
        for token in tokens:
            if token == marker:
                occurrences = body.count(marker)
            body = body.replace(token, " ")
        # Between the markers of one line only separators may remain.
        if occurrences and not body.strip(_TRAILING):
            answers += occurrences
    return answers


def marker_answered(text: str, marker: str, *, alongside: Iterable[str] = ()) -> bool:
    """Return whether a line of ``text`` holds ``marker`` and nothing else.

    ``alongside`` lists other markers the same reply was asked to carry; they
    may share the marker's line.
    """

    return marker_answer_count(text, marker, alongside=alongside) > 0


def any_marker_answered(texts: list[str], marker: str) -> bool:
    return any(marker_answered(text, marker) for text in texts)
