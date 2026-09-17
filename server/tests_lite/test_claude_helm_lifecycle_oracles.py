"""Claude Helm lifecycle oracles judge Claude's own transcript turn boundaries.

The row shapes mirror claude 2.1.273 transcripts: a Runtime Host send arrives
as a ``user`` row wrapped in ``<channel>``, a steer delivered inside the active
turn arrives as a ``queued_command`` attachment (or, from claude 2.1.274, as the
lifecycle hook's ``hook_additional_context``), and every turn closes with a
``system/turn_duration`` row.
"""

from __future__ import annotations

from zerg.qa.claude_helm_lifecycle import abort_stopped_turn
from zerg.qa.claude_helm_lifecycle import lifecycle_assertions
from zerg.qa.claude_helm_lifecycle import negative_control_verdict
from zerg.qa.claude_helm_lifecycle import steer_landed_in_turn

STEP = "lh_claude_step_x"
STEERED = "LONGHOUSE_CLAUDE_STEERED_x"
DONE = "LONGHOUSE_CLAUDE_UNSTEERED_x"


def _prompt(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": f'<channel source="longhouse-channel">\n{text}\n</channel>'}}


def _queued(text: str) -> dict:
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": f'<channel intent="steer">{text}</channel>'}}


def _bash(command: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


def _text(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _end(timestamp: str = "2026-09-16T19:40:00Z") -> dict:
    return {"type": "system", "subtype": "turn_duration", "timestamp": timestamp}


def _steer(rows: list[dict]) -> dict:
    return steer_landed_in_turn(
        rows,
        prompt_marker=f"{STEP}_1",
        steer_marker=STEERED,
        steered_marker=STEERED,
        done_marker=DONE,
        later_step_command=f"{STEP}_3",
    )


def _task() -> dict:
    return _prompt(f"`sleep 8; echo {STEP}_1`, `sleep 8; echo {STEP}_2`, `sleep 8; echo {STEP}_3`, then {DONE}")


def test_steer_inside_the_active_turn_passes() -> None:
    rows = [_task(), _bash(f"sleep 8; echo {STEP}_1"), _queued(f"Reply with exactly {STEERED}"), _text(f"Stopped.\n\n{STEERED}"), _end()]

    verdict = _steer(rows)

    assert verdict["passed"] is True
    assert verdict["failure_code"] is None


def _hook_steer(text: str, event: str = "PostToolUse") -> dict:
    # claude 2.1.274: the Machine Agent delivers an active-turn steer through the
    # lifecycle hook, recorded as a hook_additional_context attachment.
    return {
        "type": "attachment",
        "attachment": {
            "type": "hook_additional_context",
            "hookEvent": event,
            "content": [f'The user of this session sent this steer from Longhouse while you were working: "{text}".'],
        },
    }


def test_steer_delivered_by_the_lifecycle_hook_inside_the_turn_passes() -> None:
    rows = [_task(), _bash(f"sleep 8; echo {STEP}_1"), _hook_steer(f"Reply with exactly {STEERED}"), _text(STEERED), _end()]

    verdict = _steer(rows)

    assert verdict["passed"] is True
    assert verdict["steer_delivered_in_target_turn"] is True


def test_hook_steer_the_model_ignored_is_rejected() -> None:
    rows = [
        _task(),
        _bash(f"sleep 8; echo {STEP}_1"),
        _hook_steer(f"Reply with exactly {STEERED}"),
        _bash(f"sleep 8; echo {STEP}_2"),
        _hook_steer(f"Reply with exactly {STEERED}"),
        _bash(f"sleep 8; echo {STEP}_3"),
        _text(DONE),
        _end(),
    ]

    verdict = _steer(rows)

    assert verdict["passed"] is False
    assert verdict["failure_code"] == "steer_did_not_change_course"


def test_unrelated_hook_context_is_not_a_steer_delivery() -> None:
    rows = [_task(), _bash(f"sleep 8; echo {STEP}_1"), _hook_steer("something else"), _text(STEERED), _end()]

    verdict = _steer(rows)

    assert verdict["passed"] is False
    assert verdict["steer_delivered_in_target_turn"] is False


def test_queued_follow_up_after_the_turn_is_rejected() -> None:
    rows = [
        _task(),
        _bash(f"sleep 8; echo {STEP}_1"),
        _bash(f"sleep 8; echo {STEP}_2"),
        _bash(f"sleep 8; echo {STEP}_3"),
        _text(DONE),
        _end(),
        _prompt(f"Reply with exactly {STEERED}"),
        _text(STEERED),
        _end(),
    ]

    verdict = _steer(rows)

    assert verdict["passed"] is False
    assert verdict["failure_code"] == "steer_delivered_as_followup"


def test_steer_that_did_not_change_course_is_rejected() -> None:
    rows = [
        _task(),
        _bash(f"sleep 8; echo {STEP}_1"),
        _queued(STEERED),
        _bash(f"sleep 8; echo {STEP}_3"),
        _text(f"{DONE} {STEERED}"),
        _end(),
    ]

    assert _steer(rows)["failure_code"] == "steer_did_not_change_course"


def test_unfinished_target_turn_is_never_judged_a_pass() -> None:
    rows = [_task(), _bash(f"sleep 8; echo {STEP}_1"), _queued(STEERED), _text(STEERED)]

    assert _steer(rows)["failure_code"] == "steer_target_turn_never_completed"


def _abort(rows: list[dict], *, interrupted_at: float) -> dict:
    return abort_stopped_turn(
        rows,
        prompt_marker="lh_claude_progress_x",
        forbidden_marker="FORBIDDEN_x",
        interrupted_at=interrupted_at,
        tool_seconds=45,
    )


def test_abort_that_ends_the_turn_early_without_the_reply_passes() -> None:
    rows = [
        _prompt("run lh_claude_progress_x then reply FORBIDDEN_x"),
        _bash("for i ...lh_claude_progress_x"),
        _end("2026-09-16T19:40:04Z"),
    ]

    verdict = _abort(rows, interrupted_at=1789587600.0)

    assert verdict["passed"] is True
    assert verdict["turn_stop_latency_seconds"] == 4.0


def test_killed_tool_that_the_model_works_around_is_not_a_stop() -> None:
    rows = [
        _prompt("run lh_claude_progress_x then reply FORBIDDEN_x"),
        _bash("for i ...lh_claude_progress_x"),
        {"type": "user", "timestamp": "2026-09-16T19:40:01Z", "message": {"content": [{"type": "tool_result", "is_error": True}]}},
        _bash("wc -l lh_claude_progress_x.txt"),
        {"type": "user", "timestamp": "2026-09-16T19:40:03Z", "message": {"content": [{"type": "tool_result", "is_error": False}]}},
        _text("The command was killed."),
        _end("2026-09-16T19:40:10Z"),
    ]

    verdict = _abort(rows, interrupted_at=1789587600.0)

    assert verdict["passed"] is False
    assert verdict["tools_executed_after_interrupt"] == 1


def test_marker_said_after_a_killed_tool_is_still_a_stop() -> None:
    """A literal-minded model says the promised reply when its tool is killed.

    The tool errored, nothing ran after, and the turn ended early, so the work
    was stopped: the wrap-up wording is not evidence the abort failed.
    """

    rows = [
        _prompt("run lh_claude_progress_x then reply FORBIDDEN_x"),
        _bash("for i ...lh_claude_progress_x"),
        {"type": "user", "timestamp": "2026-09-16T19:40:03Z", "message": {"content": [{"type": "tool_result", "is_error": True}]}},
        _text("FORBIDDEN_x"),
        _end("2026-09-16T19:40:05Z"),
    ]

    verdict = _abort(rows, interrupted_at=1789587600.0)

    assert verdict["passed"] is True
    assert verdict["forbidden_reply_produced"] is True
    assert verdict["long_tool_completed"] is False


def test_marker_said_after_the_tool_completed_is_rejected() -> None:
    """The same wording after a *successful* tool means the work was not stopped."""

    rows = [
        _prompt("run lh_claude_progress_x then reply FORBIDDEN_x"),
        _bash("for i ...lh_claude_progress_x"),
        {"type": "user", "timestamp": "2026-09-16T19:39:59Z", "message": {"content": [{"type": "tool_result", "is_error": False}]}},
        _text("FORBIDDEN_x"),
        _end("2026-09-16T19:40:04Z"),
    ]

    verdict = _abort(rows, interrupted_at=1789587600.0)

    assert verdict["passed"] is False
    assert verdict["failure_code"] == "abort_did_not_stop_turn"
    assert verdict["forbidden_reply_after_completed_tool"] is True


def test_noop_interrupt_that_lets_the_tool_finish_is_rejected() -> None:
    rows = [
        _prompt("run lh_claude_progress_x then reply FORBIDDEN_x"),
        _bash("for i ...lh_claude_progress_x"),
        _text("FORBIDDEN_x"),
        _end("2026-09-16T19:40:44Z"),
    ]

    verdict = _abort(rows, interrupted_at=1789587600.0)

    assert verdict["passed"] is False
    assert verdict["failure_code"] == "abort_did_not_stop_turn"


def _recovery_abort(rows: list[dict]) -> dict:
    return abort_stopped_turn(
        rows,
        prompt_marker="lh_claude_progress_x",
        forbidden_marker="FORBIDDEN_x",
        interrupted_at=1789587600.0,
        tool_seconds=45,
        recovery_marker="LONGHOUSE_CLAUDE_RECOVERED_x",
    )


def test_abort_passes_only_when_a_following_turn_completes() -> None:
    stopped = [
        _prompt("run lh_claude_progress_x then reply FORBIDDEN_x"),
        _bash("for i ...lh_claude_progress_x"),
        _end("2026-09-16T19:40:04Z"),
    ]
    recovered = [
        _prompt("Reply with exactly LONGHOUSE_CLAUDE_RECOVERED_x"),
        _text("LONGHOUSE_CLAUDE_RECOVERED_x"),
        _end("2026-09-16T19:40:09Z"),
    ]

    verdict = _recovery_abort(stopped + recovered)

    assert verdict["passed"] is True
    assert verdict["following_turn_completed"] is True


def test_abort_that_stops_the_turn_but_never_recovers_is_rejected() -> None:
    stopped = [
        _prompt("run lh_claude_progress_x then reply FORBIDDEN_x"),
        _bash("for i ...lh_claude_progress_x"),
        _end("2026-09-16T19:40:04Z"),
    ]
    # The recovery prompt was accepted but its turn never completed.
    never_answered = [_prompt("Reply with exactly LONGHOUSE_CLAUDE_RECOVERED_x")]

    assert _recovery_abort(stopped)["failure_code"] == "abort_following_turn_missing"
    verdict = _recovery_abort(stopped + never_answered)
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "abort_following_turn_missing"
    assert verdict["following_turn_completed"] is False


def test_negative_control_passes_only_when_the_fault_fired_and_was_caught() -> None:
    lifecycle = {
        "launch_registration": {"passed": True},
        "send_idle": {"passed": True},
        "steer_active": {"passed": False, "failure_code": "steer_delivered_as_followup"},
    }
    fired = {"fault": "claude_steer_after_turn"}

    assert negative_control_verdict(lifecycle, fault="claude_steer_after_turn", fault_receipt=fired)["status"] == "pass"
    assert negative_control_verdict(lifecycle, fault="claude_steer_after_turn", fault_receipt=None)["status"] == "inconclusive"
    lifecycle["steer_active"] = {"passed": True, "failure_code": None}
    assert negative_control_verdict(lifecycle, fault="claude_steer_after_turn", fault_receipt=fired)["status"] == "fail"


def test_lifecycle_assertions_require_completion_and_cleanup() -> None:
    lifecycle = {key: {"passed": True} for key in ("launch_registration", "send_idle", "steer_active", "abort_native", "terminate_owned")}

    assert all(lifecycle_assertions(lifecycle, completed=True, cleanup_ok=True).values())
    assert lifecycle_assertions(lifecycle, completed=True, cleanup_ok=False)["claude_helm_terminate_owned"] is False
    assert lifecycle_assertions(lifecycle, completed=False, cleanup_ok=True)["claude_helm_steer_active"] is False
