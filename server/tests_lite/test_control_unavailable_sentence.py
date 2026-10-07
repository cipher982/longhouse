"""Precedence of the served "why can't I send" sentence.

Web and iOS render ``capabilities.composer_disabled_reason`` verbatim, so the
ordering lives here only: closed, then an unreachable machine, then an ended
Helm run, then Console blockers, then reattach, then the connection state.
"""

from __future__ import annotations

from typing import Any

import pytest

from zerg.services.session_state_contract import SessionStateFacts
from zerg.services.session_views import _control_unavailable_sentence


def _action(state: str = "unavailable", reason: str | None = None) -> dict[str, Any]:
    return {"state": state, "reason": reason}


def _facts(
    *,
    mode: str = "helm",
    closed: bool = False,
    host: str = "online",
    connection: str = "unknown",
    run_lifecycle: str | None = None,
    reattach: str = "unavailable",
    resume: str = "unavailable",
    start_turn_reason: str | None = None,
) -> SessionStateFacts:
    return SessionStateFacts.model_validate(
        {
            "mode": mode,
            "disposition": {"state": "closed" if closed else "open"},
            "run": {"lifecycle": run_lifecycle} if run_lifecycle else None,
            "activity": {"state": "quiescent"},
            "control": {
                "ownership": "owned",
                "connection": connection,
                "actions": {
                    "start_turn": _action(reason=start_turn_reason),
                    "send_input": _action(),
                    "interrupt": _action(),
                    "terminate": _action(),
                    "reattach": _action(reattach),
                    "resume": _action(resume),
                },
            },
            "transcript": {"convergence": "current"},
            "host": {"state": host},
            "presentation": {},
        }
    )


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (
            _facts(closed=True, host="offline", run_lifecycle="ended", reattach="available"),
            "This session is closed.",
        ),
        (
            _facts(host="offline", run_lifecycle="ended", resume="available"),
            "The machine running this session is offline. Sending resumes when it reconnects.",
        ),
        (
            _facts(run_lifecycle="ended", resume="available", reattach="available"),
            "This session's run has ended. Resume it to keep going.",
        ),
        (_facts(run_lifecycle="ended"), "This session's run has ended."),
        (
            _facts(host="offline", reattach="available"),
            "The machine running this session is offline. Sending resumes when it reconnects.",
        ),
        (
            _facts(reattach="available", connection="degraded"),
            "Longhouse isn't attached to this session. Reattach to steer it from here.",
        ),
        (_facts(connection="degraded"), "Longhouse's control link to this session stopped answering."),
        (_facts(connection="disconnected"), "Longhouse's control path to this session is closed."),
        (_facts(), "Longhouse can't confirm the control link to this session right now."),
        (
            _facts(mode="console", start_turn_reason="machine_offline"),
            "The machine running this session is offline. Sending resumes when it reconnects.",
        ),
        (
            _facts(mode="console", start_turn_reason="adapter_unavailable"),
            "This session's machine isn't accepting new turns.",
        ),
        (
            _facts(mode="console", start_turn_reason="execution_target_missing"),
            "Longhouse has no machine and folder recorded to run this in.",
        ),
        (
            _facts(mode="console", start_turn_reason="something_else"),
            "Longhouse cannot start a new turn on this session right now.",
        ),
    ],
)
def test_control_unavailable_sentence_precedence(facts: SessionStateFacts, expected: str) -> None:
    assert _control_unavailable_sentence(facts) == expected
