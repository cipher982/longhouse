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


def _control(control: str, *, provider: str = "pi", assertions: dict, observation: dict, fired: bool = True) -> dict:
    from zerg.qa.pi_family_turn_oracle import negative_control_verdict
    from zerg.qa.pi_family_turn_oracle import fault_name

    receipts = [{"fault": fault_name(provider, control), "session_id": "sess-1"}] if fired else []
    return negative_control_verdict(
        control,
        provider=provider,
        assertions=assertions,
        observation=observation,
        fault_receipts=receipts,
        session_id="sess-1",
    )


def test_terminate_control_passes_when_the_oracle_catches_surviving_owners() -> None:
    # The terminate no-op fault: the command is accepted and the recorded
    # owners keep running. The oracle must reject terminate_owned for that
    # exact reason, or the Interrupt chip is not fail-closed.
    verdict = _control(
        "terminate",
        assertions={
            "pi_helm_send_idle": True,
            "pi_helm_follow_up_native": True,
            "pi_helm_steer_active": True,
            "pi_helm_abort_native": True,
            "pi_helm_terminate_owned": False,
        },
        observation={"terminate_verdict": {"code": "terminate_left_owners_alive"}},
    )
    assert verdict["status"] == "pass", verdict
    assert verdict["target_assertion"] == "pi_helm_terminate_owned"


def test_terminate_control_is_not_a_pass_when_the_oracle_misses_it() -> None:
    # A weak oracle would call terminate proven while the owners survived.
    verdict = _control(
        "terminate",
        assertions={
            "pi_helm_launch_registration": True,
            "pi_helm_send_idle": True,
            "pi_helm_terminate_owned": True,
        },
        observation={"terminate_verdict": {"code": None}},
    )
    assert verdict["status"] != "pass", verdict
    assert verdict["target_rejected"] is False


def test_send_control_does_not_require_its_own_target_as_a_precondition() -> None:
    # send_idle is the target, so requiring it to hold would make the control
    # unsatisfiable - the same defect that made the OMP Console check
    # impossible to pass.
    verdict = _control(
        "send",
        assertions={
            "pi_helm_launch_registration": True,
            "pi_helm_send_idle": False,
        },
        observation={"send_turn_verdict": {"code": "send_accepted_without_a_turn"}},
    )
    assert verdict["preconditions_held"] is True, verdict
    assert verdict["status"] == "pass", verdict


def test_a_control_whose_fault_never_fired_is_inconclusive_not_a_pass() -> None:
    verdict = _control(
        "terminate",
        assertions={
            "pi_helm_launch_registration": True,
            "pi_helm_send_idle": True,
            "pi_helm_terminate_owned": False,
        },
        observation={"terminate_verdict": {"code": "terminate_left_owners_alive"}},
        fired=False,
    )
    assert verdict["status"] == "inconclusive", verdict


def test_terminate_control_does_not_require_evidence_the_fault_prevents() -> None:
    """OMP's launch_registration cannot be a terminate precondition.

    It is not a launch check: it ANDs settlement and a four-phase control
    identity that needs the cold_resume and final receipts. A no-op terminate
    keeps the session alive, so OMP can never reach that phase, and requiring
    it made the control inconclusive by construction -- fault fired, target
    rejected with the expected typed code, and still no pass (2026-09-19).

    The healthy prefix is proven by the pre-terminate steps instead, which
    already require channel binding and native evidence.
    """
    verdict = _control(
        "terminate",
        provider="omp",
        assertions={
            # False only because the fault stops the run before settlement.
            "omp_helm_launch_registration": False,
            "omp_helm_send_idle": True,
            "omp_helm_follow_up_native": True,
            "omp_helm_steer_active": True,
            "omp_helm_abort_native": True,
            "omp_helm_terminate_owned": False,
        },
        observation={"terminate_verdict": {"code": "terminate_left_owners_alive"}},
    )
    assert verdict["preconditions_held"] is True, verdict
    assert verdict["status"] == "pass", verdict


def test_terminate_control_still_needs_a_healthy_prefix() -> None:
    # A run whose steer was already broken cannot attribute a terminate failure
    # to the fault, so it stays inconclusive rather than certifying the chip.
    verdict = _control(
        "terminate",
        provider="omp",
        assertions={
            "omp_helm_launch_registration": True,
            "omp_helm_send_idle": True,
            "omp_helm_follow_up_native": True,
            "omp_helm_steer_active": False,
            "omp_helm_abort_native": True,
            "omp_helm_terminate_owned": False,
        },
        observation={"terminate_verdict": {"code": "terminate_left_owners_alive"}},
    )
    assert verdict["preconditions_held"] is False, verdict
    assert verdict["status"] == "inconclusive", verdict
