from __future__ import annotations

from zerg.qa.pi_family_turn_oracle import abort_then_send_verdict
from zerg.qa.pi_family_turn_oracle import steer_turn_verdict

TASK = "TASK_abc"
DONE = "DONE_abc"
STEER = "STEER_abc"
AFTER = "AFTER_abc"


def _entry(entry_id: str, parent: str | None, role: str, *, text: str = "", stop: str | None = None, tool: bool = False) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "toolCall", "arguments": {"command": f"sleep 3 && echo {TASK}-step"}})
    message: dict = {"role": role, "content": content}
    if stop:
        message["stopReason"] = stop
    return {"type": "message", "id": entry_id, "parentId": parent, "message": message}


def _task_prefix() -> list[dict]:
    return [
        {"type": "session", "id": "s"},
        _entry("u1", "s", "user", text=f"{TASK}: run the steps, then reply with {DONE}"),
        _entry("a1", "u1", "assistant", stop="toolUse", tool=True),
        _entry("t1", "a1", "toolResult", text="step1"),
    ]


def test_steer_inside_the_task_turn_passes() -> None:
    entries = [
        *_task_prefix(),
        _entry("u2", "t1", "user", text=f"Stop and reply only with {STEER}"),
        _entry("a2", "u2", "assistant", text=STEER, stop="stop"),
    ]
    verdict = steer_turn_verdict(entries, task_marker=TASK, steer_marker=STEER, task_done_marker=DONE)
    assert verdict["passed"] is True, verdict


def test_steer_delivered_after_the_task_finished_is_rejected() -> None:
    # The queued follow-up shape: the task turn ends with `stop` before the
    # steer text is delivered, and the steer marker is still answered.
    entries = [
        *_task_prefix(),
        _entry("a2", "t1", "assistant", text=DONE, stop="stop"),
        _entry("u2", "a2", "user", text=f"Stop and reply only with {STEER}"),
        _entry("a3", "u2", "assistant", text=STEER, stop="stop"),
    ]
    verdict = steer_turn_verdict(entries, task_marker=TASK, steer_marker=STEER, task_done_marker=DONE)
    assert verdict["passed"] is False
    assert verdict["code"] == "steer_delivered_as_queued_follow_up"


def test_steer_that_does_not_change_course_is_rejected() -> None:
    entries = [
        *_task_prefix(),
        _entry("u2", "t1", "user", text=f"Stop and reply only with {STEER}"),
        _entry("a2", "u2", "assistant", text=STEER, stop="toolUse", tool=True),
        _entry("t2", "a2", "toolResult", text="step2"),
        _entry("a3", "t2", "assistant", text=DONE, stop="stop"),
    ]
    verdict = steer_turn_verdict(entries, task_marker=TASK, steer_marker=STEER, task_done_marker=DONE)
    assert verdict["code"] == "original_task_completed_after_steer"


def test_abort_followed_by_a_completed_turn_passes() -> None:
    entries = [
        *_task_prefix(),
        _entry("a2", "t1", "assistant", stop="aborted"),
        _entry("u2", "a2", "user", text=f"Reply only with {AFTER}"),
        _entry("a3", "u2", "assistant", text=AFTER, stop="stop"),
    ]
    verdict = abort_then_send_verdict(entries, task_marker=TASK, task_done_marker=DONE, after_marker=AFTER)
    assert verdict["passed"] is True, verdict


def test_noop_abort_is_rejected() -> None:
    # The abort acknowledged but never reached the provider: the task finishes.
    entries = [
        *_task_prefix(),
        _entry("a2", "t1", "assistant", text=DONE, stop="stop"),
        _entry("u2", "a2", "user", text=f"Reply only with {AFTER}"),
        _entry("a3", "u2", "assistant", text=AFTER, stop="stop"),
    ]
    verdict = abort_then_send_verdict(entries, task_marker=TASK, task_done_marker=DONE, after_marker=AFTER)
    assert verdict["code"] == "abort_did_not_stop_active_turn"


def test_abort_without_a_following_turn_is_rejected() -> None:
    entries = [*_task_prefix(), _entry("a2", "t1", "assistant", stop="aborted")]
    verdict = abort_then_send_verdict(entries, task_marker=TASK, task_done_marker=DONE, after_marker=AFTER)
    assert verdict["code"] == "turn_after_abort_missing"


def test_later_turn_from_a_backgrounded_job_does_not_void_the_steer() -> None:
    # OMP backgrounds the running command for the steer, answers it, then
    # re-prompts when that job finishes. The steered turn still changed course.
    entries = [
        *_task_prefix(),
        _entry("u2", "t1", "user", text=f"Stop and reply only with {STEER}"),
        _entry("a2", "u2", "assistant", text=STEER, stop="stop"),
        {"type": "custom_message", "id": "j1", "parentId": "a2"},
        _entry("a3", "j1", "assistant", stop="toolUse", tool=True),
        _entry("t3", "a3", "toolResult", text="step3"),
        _entry("a4", "t3", "assistant", text=DONE, stop="stop"),
    ]
    verdict = steer_turn_verdict(entries, task_marker=TASK, steer_marker=STEER, task_done_marker=DONE)
    assert verdict["passed"] is True, verdict
    assert verdict["task_done_rows"] == 1
