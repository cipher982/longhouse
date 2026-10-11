"""What a wait was waiting on when it expired.

A producer drives a real provider through Longhouse and waits at every step: for
the model's reply, for the provider's process, for Longhouse to ship and serve
what the provider wrote. When one of those waits expires the producer fails
without having judged anything, and whoever reads the failure needs to know
which thing never arrived. The wording of a timeout cannot say that. "Timed out
waiting for the reply in the hosted archive" is the model that stalled, or
Longhouse's ingest that never served a reply the model gave, and the two are
different failures with different owners.

So the wait itself says it. A wait that expires raises an error marked with one
of five kinds, named for what was awaited, never for who is at fault:

``provider_turn``
    The provider's model turn: a reply, a tool call, a tool result or the end
    of the turn, read where the provider itself writes it (its transcript, its
    output stream, its own hooks). Nothing of Longhouse stands between the
    model and that record.
``provider_process``
    The provider's own process: that it starts, shows its prompt, or exits.
``longhouse_engine``
    Longhouse on the machine: the engine, a bridge, an adapter claim, a hook
    relay, the local state the engine writes.
``longhouse_ingest``
    Longhouse shipping and serving what the provider already wrote: the hosted
    archive, a served projection, Runtime Host convergence.
``runtime_host``
    The Runtime Host answering a request or holding control state.

A wait declares the kind it can observe and no other. A wait that covers two
things (the provider must answer, then Longhouse must serve the answer) looks
at which side stopped before it raises: the provider's own transcript shows
whether the model answered. A wait that cannot tell declares nothing, and its
failure is then read as it was before there were declarations.

The producer's failure artifact carries the kind as ``waited_on`` beside
``failure_code`` and ``error`` (``failure_fields``). The provider factory reads
it there: it retries a wait on the provider and no other, and it classes the
failure without reading the message.
"""

from __future__ import annotations

from typing import TypeVar

PROVIDER_TURN = "provider_turn"
PROVIDER_PROCESS = "provider_process"
LONGHOUSE_ENGINE = "longhouse_engine"
LONGHOUSE_INGEST = "longhouse_ingest"
RUNTIME_HOST = "runtime_host"
WAIT_KINDS = (PROVIDER_TURN, PROVIDER_PROCESS, LONGHOUSE_ENGINE, LONGHOUSE_INGEST, RUNTIME_HOST)

# The field of a producer's failure artifact that carries the kind.
FIELD = "waited_on"

_Error = TypeVar("_Error", bound=BaseException)


def expired(error: _Error, waited_on: str) -> _Error:
    """Mark ``error`` as a wait on ``waited_on`` that expired, and return it.

    The error keeps its type and message, so a caller that catches or matches
    it is undisturbed: ``raise expired(RuntimeError("timed out ..."), PROVIDER_TURN)``.
    """

    if waited_on not in WAIT_KINDS:
        raise ValueError(f"unknown wait kind: {waited_on!r}")
    error.waited_on = waited_on  # type: ignore[attr-defined]
    return error


def waited_on(error: BaseException | None) -> str | None:
    """The kind ``error`` was marked with, or the kind of the error it was raised from.

    Only an explicit ``raise ... from`` is followed. An error raised while
    another was being handled is a different failure, and inheriting the first
    one's kind would declare something nobody observed.
    """

    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        kind = getattr(error, "waited_on", None)
        if isinstance(kind, str) and kind in WAIT_KINDS:
            return kind
        seen.add(id(error))
        error = error.__cause__
    return None


def failure_fields(error: BaseException | None) -> dict[str, str]:
    """What a failure artifact adds for ``error``: its kind, when it has one."""

    kind = waited_on(error)
    return {FIELD: kind} if kind is not None else {}
