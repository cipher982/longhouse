"""Oracle self-tests for zerg.qa.opencode_helm_lifecycle.

The rows mirror OpenCode 1.17.20's message store as observed live: a steer that
lands inside the running turn versus the same text delivered after the turn
ended. The live producer's negative controls prove the same rejection against a
real provider; these keep the oracle honest between those runs.
"""

from __future__ import annotations

from zerg.qa.opencode_helm_lifecycle import abort_observation
from zerg.qa.opencode_helm_lifecycle import failure_codes
from zerg.qa.opencode_helm_lifecycle import opencode_helm_lifecycle_assertions
from zerg.qa.opencode_helm_lifecycle import steer_observation

TASK = "Run these shell commands ... reply with exactly DONE_X."
STEER = "STOP. Change of plan ... Reply with only STEER_X."


def _user(message_id: str, text: str) -> dict:
    return {
        "id": message_id,
        "role": "user",
        "parent_id": None,
        "finish": None,
        "error": None,
        "created": 0,
        "completed": None,
        "text": text,
        "tool_commands": [],
    }


def _assistant(
    parent: str, *, finish: str, text: str = "", commands: tuple[str, ...] = (), completed_ms: int = 0, error: str | None = None
) -> dict:
    return {
        "id": f"a-{parent}-{finish}-{completed_ms}",
        "role": "assistant",
        "parent_id": parent,
        "finish": finish,
        "error": error,
        "created": 0,
        "completed": completed_ms,
        "text": text,
        "tool_commands": list(commands),
    }


def _busy_samples(start: float, end: float, *, idle_between: tuple[float, float] | None = None) -> list[dict]:
    samples = []
    t = start
    while t < end:
        busy = not (idle_between and idle_between[0] <= t < idle_between[1])
        samples.append({"t": round(t, 2), "busy": busy})
        t += 0.5
    return samples


def _steer(rows: list[dict], samples: list[dict]) -> dict:
    return steer_observation(
        rows, task_text=TASK, steer_text=STEER, completion_marker="DONE_X", steer_marker="STEER_X", samples=samples, steer_posted_at=100.0
    )


def _healthy(observation_overrides: dict) -> dict:
    observation = {
        "launch": {
            "session_id": "s",
            "provider_session_id": "p",
            "run_id": "r",
            "connection_id": 1,
            "tui_ready": True,
            "runtime_input_accepted": True,
        },
        "send": {"dispatch_accepted": True, "idle_before_send": True, "answered": True},
        "abort": {
            "dispatch_accepted": True,
            "task_completed": False,
            "completion_marker_seen": False,
            "final_task_step_ran": False,
            "idle_after_abort": True,
            "follow_up_answered": True,
        },
        "terminate": {"dispatch_accepted": True, "launcher_exited": True, "cleanup_verified": True, "forced_cleanup": False},
    }
    observation.update(observation_overrides)
    return observation


def test_steer_inside_the_running_turn_passes() -> None:
    rows = [
        _user("u1", TASK),
        _assistant("u1", finish="tool-calls", commands=("sleep 4; cat steer1.txt",), completed_ms=99_000),
        _assistant("u1", finish="tool-calls", commands=("sleep 4; cat steer2.txt",), completed_ms=104_000),
        _user("u2", STEER),
        _assistant("u2", finish="stop", text="STEER_X", completed_ms=108_000),
    ]
    steer = {"dispatch_accepted": True, **_steer(rows, _busy_samples(100.0, 110.0))}
    assertions = opencode_helm_lifecycle_assertions(_healthy({"steer": steer}))

    assert assertions["opencode_helm_steer_active"] is True
    assert steer["queued_follow_up_shape"] is False


def test_steer_delivered_after_the_turn_ends_is_rejected_as_queued_follow_up() -> None:
    rows = [
        _user("u1", TASK),
        *(
            _assistant("u1", finish="tool-calls", commands=(f"sleep 4; cat steer{i}.txt",), completed_ms=95_000 + i * 4000)
            for i in range(1, 6)
        ),
        _assistant("u1", finish="stop", text="DONE_X", completed_ms=118_000),
        _user("u2", STEER),
        _assistant("u2", finish="stop", text="STEER_X", completed_ms=123_000),
    ]
    samples = _busy_samples(100.0, 125.0, idle_between=(118.5, 120.0))
    steer = {"dispatch_accepted": True, **_steer(rows, samples)}
    observation = _healthy({"steer": steer})
    assertions = opencode_helm_lifecycle_assertions(observation)

    assert assertions["opencode_helm_steer_active"] is False
    assert failure_codes(assertions, observation)["opencode_helm_steer_active"] == "steer_delivered_as_queued_follow_up"


def test_idle_gap_alone_marks_the_queued_shape() -> None:
    rows = [
        _user("u1", TASK),
        _assistant("u1", finish="tool-calls", commands=("sleep 4; cat steer1.txt",), completed_ms=99_000),
        _user("u2", STEER),
        _assistant("u2", finish="stop", text="STEER_X", completed_ms=112_000),
    ]
    steer = _steer(rows, _busy_samples(100.0, 113.0, idle_between=(104.0, 105.0)))

    assert steer["idle_gap_before_steer_answer"] is True
    assert steer["queued_follow_up_shape"] is True


def test_abort_that_does_nothing_is_typed() -> None:
    rows = [
        _user("u3", "ABORT TASK"),
        *(_assistant("u3", finish="tool-calls", commands=(f"sleep 4; cat abort{i}.txt",)) for i in range(1, 6)),
        _assistant("u3", finish="stop", text="ABORT_DONE"),
        _user("u4", "FOLLOW"),
        _assistant("u4", finish="stop", text="FOLLOW_X"),
    ]
    abort = {
        "dispatch_accepted": True,
        **abort_observation(
            rows,
            task_text="ABORT TASK",
            completion_marker="ABORT_DONE",
            follow_text="FOLLOW",
            follow_marker="FOLLOW_X",
            idle_after_abort=True,
        ),
    }
    observation = _healthy({"abort": abort, "steer": {}})
    assertions = opencode_helm_lifecycle_assertions(observation)

    assert assertions["opencode_helm_abort_native"] is False
    assert failure_codes(assertions, observation)["opencode_helm_abort_native"] == "abort_did_not_stop_active_turn"


def test_abort_requires_a_following_turn_on_the_same_owner() -> None:
    rows = [
        _user("u3", "ABORT TASK"),
        _assistant("u3", finish="tool-calls", commands=("sleep 4; cat abort1.txt",), error="MessageAbortedError"),
        _user("u4", "FOLLOW"),
    ]
    abort = {
        "dispatch_accepted": True,
        **abort_observation(
            rows,
            task_text="ABORT TASK",
            completion_marker="ABORT_DONE",
            follow_text="FOLLOW",
            follow_marker="FOLLOW_X",
            idle_after_abort=True,
        ),
    }
    observation = _healthy({"abort": abort, "steer": {}})
    assertions = opencode_helm_lifecycle_assertions(observation)

    assert assertions["opencode_helm_abort_native"] is False
    assert failure_codes(assertions, observation)["opencode_helm_abort_native"] == "post_abort_turn_not_completed"


def test_terminate_that_needed_forced_cleanup_fails() -> None:
    observation = _healthy(
        {"terminate": {"dispatch_accepted": True, "launcher_exited": True, "cleanup_verified": True, "forced_cleanup": True}}
    )

    assert opencode_helm_lifecycle_assertions(observation)["opencode_helm_terminate_owned"] is False


def test_a_send_accepted_and_never_answered_is_typed_for_its_control() -> None:
    # The send no-op fault: the caller is told the message landed and no turn
    # ever starts. Without this typed code the Launch chip has no control that
    # can prove its send edge fails.
    observation = _healthy({"send": {"dispatch_accepted": True, "idle_before_send": True, "answered": False}})
    assertions = opencode_helm_lifecycle_assertions(observation)

    assert assertions["opencode_helm_send_idle"] is False
    assert failure_codes(assertions, observation)["opencode_helm_send_idle"] == "send_accepted_without_a_turn"


def test_terminate_that_left_owners_alive_is_typed_for_its_control() -> None:
    # The terminate no-op fault: accepted, and only forced cleanup got rid of
    # the owners. The Interrupt chip depends on terminate, so this must be
    # distinguishable from an unverified cleanup.
    observation = _healthy(
        {"terminate": {"dispatch_accepted": True, "launcher_exited": False, "cleanup_verified": True, "forced_cleanup": True}}
    )
    assertions = opencode_helm_lifecycle_assertions(observation)

    assert assertions["opencode_helm_terminate_owned"] is False
    assert failure_codes(assertions, observation)["opencode_helm_terminate_owned"] == "terminate_left_owners_alive"
