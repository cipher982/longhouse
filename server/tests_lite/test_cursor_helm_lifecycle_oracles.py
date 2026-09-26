"""Cursor Helm lifecycle oracles judged against recorded hook shapes.

The shapes come from live cursor-agent 2026.09.10 runs: a real steer answers
inside the generation it targeted and stops the remaining steps; a single
Enter only queues, so the original generation finishes and the steer text is
answered by the next generation.
"""

import json

from zerg.qa.cursor_helm_lifecycle import failed_run_report
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
    passed_verdict = abort_stopped_generation(aborted, generation_id="c1", forbidden_marker="FORBIDDEN")
    assert passed_verdict["passed"] is True
    assert passed_verdict["failure_code"] is None
    finished_verdict = abort_stopped_generation(finished, generation_id="c1", forbidden_marker="FORBIDDEN")
    assert finished_verdict["passed"] is False
    assert finished_verdict["failure_code"] == "abort_generation_not_stopped"


def test_abort_failure_code_names_the_exact_cursor_abort_noop_shape() -> None:
    """A ^C the fault swallowed: no stop hook fires and the forbidden reply
    lands, exactly as clifford's live cursor_abort_noop runs recorded it. The
    named code is what the factory's independent verifier keys off (see
    test_abort_negative_control_reports_a_typed_target_failure_code_for_independent_verification)."""

    noop = [{"event": "afterAgentResponse", "generation_id": "c1", "text": "FORBIDDEN"}]
    assert (
        abort_stopped_generation(noop, generation_id="c1", forbidden_marker="FORBIDDEN")["failure_code"] == "abort_generation_not_stopped"
    )

    # Aborted, but the forbidden reply leaked through anyway.
    leaked = [
        {"event": "afterAgentResponse", "generation_id": "c1", "text": "FORBIDDEN"},
        {"event": "stop", "generation_id": "c1", "status": "aborted"},
    ]
    assert (
        abort_stopped_generation(leaked, generation_id="c1", forbidden_marker="FORBIDDEN")["failure_code"]
        == "abort_forbidden_response_produced"
    )


def test_abort_passes_only_when_a_following_generation_completes() -> None:
    aborted = [{"event": "stop", "generation_id": "c1", "status": "aborted"}]
    recovered = [
        {"event": "afterAgentResponse", "generation_id": "c2", "text": "RECOVERED"},
        {"event": "stop", "generation_id": "c2", "status": "completed"},
    ]
    passed = abort_stopped_generation(aborted + recovered, generation_id="c1", forbidden_marker="FORBIDDEN", recovery_marker="RECOVERED")
    assert passed["passed"] is True
    assert passed["following_turn_completed"] is True

    # The abort stopped the generation, but the surviving session never
    # finished a following turn.
    never_recovered = abort_stopped_generation(aborted, generation_id="c1", forbidden_marker="FORBIDDEN", recovery_marker="RECOVERED")
    assert never_recovered["passed"] is False
    assert never_recovered["following_turn_completed"] is False
    assert never_recovered["failure_code"] == "abort_following_turn_not_completed"
    unfinished = abort_stopped_generation(
        aborted + recovered[:1], generation_id="c1", forbidden_marker="FORBIDDEN", recovery_marker="RECOVERED"
    )
    assert unfinished["passed"] is False


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


def _abort_report(*, status: str, abort: dict) -> dict:
    return {
        "status": status,
        "lifecycle": {
            "launch_registration": {"state_ready": True, "native_binding_claimed": True, "first_reply_archived": True},
            "send_idle": {"remote_reply_archived": True},
            "steer_active": {"passed": True, "failure_code": None},
            "abort_native": abort,
        },
    }


def test_abort_negative_control_judges_the_abort_step_not_the_steer() -> None:
    receipt = {"fault": "cursor_abort_noop", "generation_id": "g1"}
    # An unsent ^C: the generation is never aborted and answers the reply the
    # oracle forbids. The abort assertion has to catch exactly that.
    caught = _abort_report(
        status="negative_control_observed",
        abort={
            "passed": False,
            "failure_code": "abort_generation_not_stopped",
            "generation_stopped_aborted": False,
            "forbidden_response_produced": True,
            "qa_fault_receipt": receipt,
        },
    )
    missed = _abort_report(
        status="negative_control_observed",
        abort={
            "passed": True,
            "generation_stopped_aborted": True,
            "forbidden_response_produced": False,
            "qa_fault_receipt": receipt,
        },
    )
    unfired = _abort_report(
        status="negative_control_observed",
        abort={"passed": False, "generation_stopped_aborted": False, "forbidden_response_produced": True, "qa_fault_receipt": None},
    )
    # The abort did stop the generation and no forbidden reply appeared, so this
    # run failed for some other reason and proves nothing about the oracle.
    unrelated = _abort_report(
        status="negative_control_observed",
        abort={
            "passed": False,
            "generation_stopped_aborted": True,
            "forbidden_response_produced": False,
            "following_turn_completed": False,
            "qa_fault_receipt": receipt,
        },
    )

    assert negative_control_verdict(caught, fault="cursor_abort_noop")["status"] == "pass"
    assert negative_control_verdict(caught, fault="cursor_abort_noop")["target_assertion"] == "cursor_helm_abort_native"
    assert negative_control_verdict(missed, fault="cursor_abort_noop")["status"] == "fail"
    assert negative_control_verdict(unfired, fault="cursor_abort_noop")["status"] == "inconclusive"
    assert negative_control_verdict(unrelated, fault="cursor_abort_noop")["status"] == "inconclusive"


def test_abort_negative_control_reports_a_typed_target_failure_code_for_independent_verification() -> None:
    """provider_factory/negative_controls.py.normalize_verdict re-derives the
    control's verdict independently instead of trusting this producer's own
    "pass" claim (candidate code cannot certify itself). Its generic branch
    requires a non-empty string `target_failure_code` to confirm the target
    assertion failed in the fault's own shape -- every other provider's abort
    oracle already reports one via `failure_code`. Cursor's abort oracle
    (`abort_stopped_generation`) previously had no `failure_code` field at
    all, so this producer's `target_detail` never carried one either: the
    independent verifier's generic branch always saw `target_failure_code` as
    missing/None and permanently recorded verdict="fail" for cursor_abort_noop
    even when this producer correctly caught the fault and self-reported
    "pass" -- exactly what clifford's factory.sqlite3 negative_control_verdicts
    table shows for the 2026-09-22 and 2026-09-25 runs (producer_claim="pass",
    independent verdict="fail"). Without a typed code here, the abort chip can
    never be independently certified regardless of how well the oracle works.
    """

    receipt = {"fault": "cursor_abort_noop", "generation_id": "g1"}
    caught = _abort_report(
        status="negative_control_observed",
        abort={
            "passed": False,
            "failure_code": "abort_generation_not_stopped",
            "generation_stopped_aborted": False,
            "forbidden_response_produced": True,
            "qa_fault_receipt": receipt,
        },
    )
    missed = _abort_report(
        status="negative_control_observed",
        abort={
            "passed": True,
            "failure_code": None,
            "generation_stopped_aborted": True,
            "forbidden_response_produced": False,
            "qa_fault_receipt": receipt,
        },
    )

    verdict = negative_control_verdict(caught, fault="cursor_abort_noop")
    assert verdict["status"] == "pass"
    assert isinstance(verdict["target_failure_code"], str) and verdict["target_failure_code"]
    assert verdict["target_failure_code"] == "abort_generation_not_stopped"

    # A passing (unfaulted) abort must not carry a failure code either.
    assert negative_control_verdict(missed, fault="cursor_abort_noop")["target_failure_code"] is None


def test_abort_control_does_not_read_the_steer_receipt() -> None:
    # A steer-shaped receipt on the steer step must not satisfy an abort control:
    # the fault has to have fired against the step being judged.
    report = _abort_report(
        status="negative_control_observed",
        abort={"passed": False, "generation_stopped_aborted": False, "forbidden_response_produced": True},
    )
    report["lifecycle"]["steer_active"]["qa_fault_receipt"] = {"fault": "cursor_abort_noop"}
    verdict = negative_control_verdict(report, fault="cursor_abort_noop")
    assert verdict["fault_fired"] is False
    assert verdict["status"] == "inconclusive"


def test_a_failed_run_keeps_the_steps_it_already_proved(tmp_path) -> None:
    """A late failure must not erase evidence the run already produced.

    Shape taken from the real 2026-09-19 factory run that timed out at the
    steer: launch_registration and send_idle had passed and cursor_pid was
    known, but the harness replaced the report with a bare status/error dict.
    lifecycle vanished, so all five assertions read false, and cursor_pid
    vanished, so session_stopped could not be proven and every cursor_helm cell
    failed as "lacks required cleanup" instead of being judged.
    """
    (tmp_path / "product-e2e.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "session_id": "b212cd01-b66b-4532-9ad9-c32de5ae9806",
                "cursor_pid": 105,
                "lifecycle": {
                    "launch_registration": {
                        "first_reply_archived": True,
                        "native_binding_claimed": True,
                        "state_ready": True,
                    },
                    "send_idle": {"remote_reply_archived": True},
                },
            }
        ),
        encoding="utf-8",
    )

    report = failed_run_report(tmp_path, RuntimeError("timed out waiting for steered Cursor generation completing"))

    assert report["cursor_pid"] == 105
    assert report["session_id"] == "b212cd01-b66b-4532-9ad9-c32de5ae9806"
    # Recovering evidence never upgrades the verdict.
    assert report["status"] == "failed"
    assert "timed out waiting for steered" in report["error"]

    assertions = lifecycle_assertions(report, cleanup_ok=False)
    assert assertions["cursor_helm_launch_registration"] is True
    assert assertions["cursor_helm_send_idle"] is True
    # The steps that never ran stay false; only their own oracles can pass them.
    assert assertions["cursor_helm_steer_active"] is False
    assert assertions["cursor_helm_abort_native"] is False
    assert assertions["cursor_helm_terminate_owned"] is False


def test_an_unreadable_report_still_yields_a_typed_failure(tmp_path) -> None:
    report = failed_run_report(tmp_path, RuntimeError("boom"))
    assert report == {"status": "failed", "error": "RuntimeError: boom"}


def test_a_persisted_pass_cannot_survive_the_failure(tmp_path) -> None:
    """The recovered status is the harness's, never the file's.

    The merge puts the failure last for exactly this reason: a report claiming
    it passed, while the run in fact raised, must not carry that claim into the
    result and unlock the steer/abort/terminate oracles.
    """
    (tmp_path / "product-e2e.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "run_lifecycle_after_teardown": "ended",
                "lifecycle": {"steer_active": {"passed": True}, "terminate_owned": {"passed": True}},
            }
        ),
        encoding="utf-8",
    )

    report = failed_run_report(tmp_path, RuntimeError("timed out"))

    assert report["status"] == "failed"
    assertions = lifecycle_assertions(report, cleanup_ok=True)
    assert assertions["cursor_helm_steer_active"] is False
    assert assertions["cursor_helm_terminate_owned"] is False
