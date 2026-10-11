"""A wait that expires says what it was waiting on."""

from __future__ import annotations

import pytest

from zerg.qa import wait_kind
from zerg.qa.wait_kind import LONGHOUSE_ENGINE
from zerg.qa.wait_kind import LONGHOUSE_INGEST
from zerg.qa.wait_kind import PROVIDER_PROCESS
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import RUNTIME_HOST
from zerg.qa.wait_kind import expired
from zerg.qa.wait_kind import failure_fields
from zerg.qa.wait_kind import waited_on

# --- the declaration itself ---------------------------------------------------


def test_an_expired_wait_keeps_its_type_and_message_and_carries_its_kind() -> None:
    error = expired(TimeoutError("timed out waiting for the reply"), PROVIDER_TURN)

    assert type(error) is TimeoutError
    assert str(error) == "timed out waiting for the reply"
    assert waited_on(error) == PROVIDER_TURN
    assert failure_fields(error) == {"waited_on": "provider_turn"}
    assert wait_kind.FIELD == "waited_on"
    assert set(wait_kind.WAIT_KINDS) == {PROVIDER_TURN, PROVIDER_PROCESS, LONGHOUSE_ENGINE, LONGHOUSE_INGEST, RUNTIME_HOST}


def test_a_kind_outside_the_vocabulary_cannot_be_declared() -> None:
    with pytest.raises(ValueError, match="unknown wait kind"):
        expired(RuntimeError("x"), "somewhere_else")
    # A stray attribute is not a declaration either.
    stray = RuntimeError("x")
    stray.waited_on = "somewhere_else"  # type: ignore[attr-defined]
    assert waited_on(stray) is None
    assert failure_fields(RuntimeError("not a wait")) == {}
    assert failure_fields(None) == {}


def test_the_kind_follows_an_explicit_cause_and_never_an_incidental_context() -> None:
    stall = expired(RuntimeError("timed out waiting for the reply"), PROVIDER_TURN)

    # A producer that adds diagnostics re-raises from the wait: still that wait.
    try:
        try:
            raise stall
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}; diagnostic={{}}") from exc
    except RuntimeError as wrapped:
        assert waited_on(wrapped) == PROVIDER_TURN

    # An error raised while the wait was being handled is a different failure.
    try:
        try:
            raise stall
        except RuntimeError:
            raise RuntimeError("cleanup could not remove the workspace")  # noqa: B904 - the point of the test
    except RuntimeError as unrelated:
        assert unrelated.__context__ is stall
        assert waited_on(unrelated) is None
