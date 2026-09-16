"""Cursor Helm lifecycle oracles judged against recorded hook shapes.

The shapes come from live cursor-agent 2026.09.10 runs: a real steer answers
inside the generation it targeted and stops the remaining steps; a single
Enter only queues, so the original generation finishes and the steer text is
answered by the next generation.
"""

from zerg.qa.cursor_helm_lifecycle import lifecycle_assertions
from zerg.qa.cursor_helm_lifecycle import negative_control_verdict
from zerg.qa.cursor_helm_product_e2e import abort_stopped_generation
from zerg.qa.cursor_helm_product_e2e import steer_landed_in_generation

STEP = "LONGHOUSE_CURSOR_STEP_x"


def _shell(generation: str, n: int) -> dict:
    return {"event": "beforeShellExecution", "generation_id": generation, "command": f"sleep 6; echo {STEP}_{n}"}


def _judge(rows: list[dict]) -> dict:
    return steer_landed_in_generation(
        rows, generation_id="g1", steered_marker="STEERED", done_marker="UNSTEERED", later_step_command=f"{STEP}_3"
    )


def test_steer_inside_target_generation_passes() -> None:
    rows = [
        _shell("g1", 1),
        _shell("g1", 2),
        {"event": "afterAgentResponse", "generation_id": "g1", "text": "Starting with step 1.\nSTEERED"},
        {"event": "stop", "generation_id": "g1", "status": "completed"},
        # The backgrounded step-2 shell finishing starts one more generation.
        {"event": "afterAgentResponse", "generation_id": "g2", "text": "The background command finished."},
        {"event": "stop", "generation_id": "g2", "status": "completed"},
    ]
    verdict = _judge(rows)
    assert verdict["passed"] is True
    assert verdict["failure_code"] is None


def test_queued_follow_up_is_rejected() -> None:
    rows = [
        _shell("g1", 1),
        _shell("g1", 2),
        _shell("g1", 3),
        {"event": "afterAgentResponse", "generation_id": "g1", "text": "UNSTEERED"},
        {"event": "stop", "generation_id": "g1", "status": "completed"},
        {"event": "afterAgentResponse", "generation_id": "g2", "text": "STEERED"},
        {"event": "stop", "generation_id": "g2", "status": "completed"},
    ]
    verdict = _judge(rows)
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "steer_delivered_as_followup"


def test_steer_that_changes_nothing_is_rejected() -> None:
    rows = [
        _shell("g1", 1),
        _shell("g1", 2),
        _shell("g1", 3),
        {"event": "afterAgentResponse", "generation_id": "g1", "text": "UNSTEERED"},
        {"event": "stop", "generation_id": "g1", "status": "completed"},
    ]
    assert _judge(rows)["failure_code"] == "steer_did_not_change_course"


def test_marker_that_still_ran_the_last_step_is_rejected() -> None:
    rows = [
        _shell("g1", 1),
        _shell("g1", 2),
        _shell("g1", 3),
        {"event": "afterAgentResponse", "generation_id": "g1", "text": "STEERED"},
        {"event": "stop", "generation_id": "g1", "status": "completed"},
    ]
    assert _judge(rows)["passed"] is False


def test_abort_requires_aborted_stop_without_response() -> None:
    aborted = [{"event": "stop", "generation_id": "c1", "status": "aborted"}]
    finished = [
        {"event": "afterAgentResponse", "generation_id": "c1", "text": "FORBIDDEN"},
        {"event": "stop", "generation_id": "c1", "status": "completed"},
    ]
    assert abort_stopped_generation(aborted, generation_id="c1", forbidden_marker="FORBIDDEN")["passed"] is True
    assert abort_stopped_generation(finished, generation_id="c1", forbidden_marker="FORBIDDEN")["passed"] is False


def _report(*, status: str, steer: dict) -> dict:
    return {
        "status": status,
        "lifecycle": {
            "launch_registration": {"state_ready": True, "native_binding_claimed": True, "first_reply_archived": True},
            "send_idle": {"remote_reply_archived": True},
            "steer_active": steer,
        },
    }


def test_negative_control_passes_only_when_fault_fired_and_oracle_caught_it() -> None:
    receipt = {"fault": "cursor_steer_queue_only", "generation_id": "g1"}
    caught = _report(
        status="negative_control_observed",
        steer={"passed": False, "failure_code": "steer_delivered_as_followup", "qa_fault_receipt": receipt},
    )
    missed = _report(status="negative_control_observed", steer={"passed": True, "failure_code": None, "qa_fault_receipt": receipt})
    unfired = _report(
        status="negative_control_observed",
        steer={"passed": False, "failure_code": "steer_delivered_as_followup", "qa_fault_receipt": None},
    )
    crashed = {"status": "failed", "error": "timeout"}

    assert negative_control_verdict(caught, fault="cursor_steer_queue_only")["status"] == "pass"
    assert negative_control_verdict(missed, fault="cursor_steer_queue_only")["status"] == "fail"
    assert negative_control_verdict(unfired, fault="cursor_steer_queue_only")["status"] == "inconclusive"
    assert negative_control_verdict(crashed, fault="cursor_steer_queue_only")["status"] == "inconclusive"


def test_steer_and_abort_assertions_require_a_passing_run() -> None:
    report = _report(status="failed", steer={"passed": True})
    assertions = lifecycle_assertions(report, cleanup_ok=True)
    assert assertions["cursor_helm_launch_registration"] is True
    assert assertions["cursor_helm_steer_active"] is False
    assert assertions["cursor_helm_terminate_owned"] is False
