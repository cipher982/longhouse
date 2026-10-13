from __future__ import annotations

import dataclasses
import importlib
import json

import pytest

from tests_lite import _pi_family_helm_fake as fake
from zerg.qa import pi_family_helm as helm
from zerg.qa.failed_results import reached_only
from zerg.qa.model_compliance import attach
from zerg.qa.pi_family_turn_oracle import ENDS_AT_TARGET
from zerg.qa.pi_family_turn_oracle import NEGATIVE_CONTROLS
from zerg.qa.pi_family_turn_oracle import abort_then_send_verdict
from zerg.qa.pi_family_turn_oracle import control_made_its_observation
from zerg.qa.pi_family_turn_oracle import control_preconditions
from zerg.qa.pi_family_turn_oracle import control_run_assertions
from zerg.qa.pi_family_turn_oracle import pi_family_steer_noncompliance
from zerg.qa.pi_family_turn_oracle import send_turn_verdict
from zerg.qa.pi_family_turn_oracle import steer_turn_verdict
from zerg.qa.pi_family_turn_oracle import step_task_prompt
from zerg.qa.pi_family_turn_oracle import task_tool_boundary
from zerg.qa.pi_family_turn_oracle import task_tool_call_boundary

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


def test_scripted_turn_boundary_can_observe_the_tool_call_before_its_result() -> None:
    task = _entry("task", None, "user", text=step_task_prompt(TASK, DONE))
    call = _entry("call", "task", "assistant", stop="toolUse", tool=True)
    entries = [task, call]

    assert task_tool_call_boundary(entries, TASK) == "call"
    assert task_tool_boundary(entries, TASK) is None

    entries.append(_entry("result", "call", "toolResult"))
    assert task_tool_call_boundary(entries, TASK) == "call"
    assert task_tool_boundary(entries, TASK) == "result"


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
    from zerg.qa.pi_family_turn_oracle import fault_name
    from zerg.qa.pi_family_turn_oracle import negative_control_verdict

    receipts = [{"fault": fault_name(provider, control), "session_id": "sess-1"}] if fired else []
    return negative_control_verdict(
        control,
        provider=provider,
        assertions=assertions,
        observation=observation,
        fault_receipts=receipts,
        session_id="sess-1",
    )


def _healthy(control: str, provider: str = "pi", **cells: bool) -> dict[str, bool]:
    """A control run in which every precondition held and the target did not, but for ``cells``."""

    target = f"{provider}_{NEGATIVE_CONTROLS[control][1]}"
    held = {**dict.fromkeys(control_preconditions(control, provider), True), target: False}
    return {**held, **{f"{provider}_helm_{name}": value for name, value in cells.items()}}


def test_terminate_control_passes_when_the_oracle_catches_surviving_owners() -> None:
    # The terminate no-op fault: the command is accepted and the recorded
    # owners keep running. The oracle must reject terminate_owned for that
    # exact reason, or the Interrupt chip is not fail-closed.
    verdict = _control(
        "terminate",
        assertions=_healthy("terminate"),
        observation={"terminate_verdict": {"code": "terminate_left_owners_alive"}},
    )
    assert verdict["status"] == "pass", verdict
    assert verdict["target_assertion"] == "pi_helm_terminate_owned"


def test_terminate_control_is_not_a_pass_when_the_oracle_misses_it() -> None:
    # A weak oracle would call terminate proven while the owners survived.
    verdict = _control(
        "terminate",
        assertions=_healthy("terminate", terminate_owned=True),
        observation={"terminate_verdict": {"code": None}},
    )
    assert verdict["status"] != "pass", verdict
    assert verdict["target_rejected"] is False


_SEND_DROPPED = {"send_turn_verdict": {"code": "send_accepted_without_a_turn"}}


def test_a_pi_send_control_does_not_certify_on_a_run_whose_launch_was_broken() -> None:
    """Pi decides its launch cell and its tool taxonomy at launch, before the first send.

    With no precondition the control certified whatever the launch had been, and since its run reports only the
    target and the preconditions the factory never saw the false cell.
    """
    for broken in ("launch_registration", "native_tool_taxonomy"):
        verdict = _control("send", assertions=_healthy("send", **{broken: False}), observation=_SEND_DROPPED)
        assert (verdict["preconditions_held"], verdict["target_rejected"], verdict["status"]) == (False, True, "inconclusive")
        # And the false cell is in what the run reports.
        assert control_run_assertions("send", "pi", _healthy("send", **{broken: False}))[f"pi_helm_{broken}"] is False
    assert _control("send", assertions=_healthy("send"), observation=_SEND_DROPPED)["status"] == "pass"


def test_an_omp_send_control_holds_its_run_to_no_cell_because_omp_has_decided_none() -> None:
    """OMP's launch cell is the whole run's: it ANDs settlement and the last turn, so a run that ends at the first
    send never decides it, and requiring it would make the control unsatisfiable. The target is not its own
    precondition either."""
    assert control_preconditions("send", "omp") == ()
    undecided = _healthy("send", "omp", launch_registration=False)
    verdict = _control("send", provider="omp", assertions=undecided, observation=_SEND_DROPPED)
    assert (verdict["preconditions_held"], verdict["status"]) == (True, "pass")
    assert control_run_assertions("send", "omp", undecided) == {"omp_helm_send_idle": False}


def test_a_send_that_was_taken_and_started_no_turn_is_told_from_a_turn_that_did_not_answer(tmp_path) -> None:
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps(_entry("u1", None, "user", text="Reply with OTHER_MARKER")) + "\n", encoding="utf-8")

    def code(marker: str, *, accepted: bool = True, answered: bool = False) -> str | None:
        return send_turn_verdict(session, marker=marker, accepted=accepted, answered=answered)["code"]

    # The provider never held the prompt: the send was dropped on the way.
    assert code("SEND_MARKER") == "send_accepted_without_a_turn"
    # It holds the prompt and no answer: a turn started, and the model is what did not answer.
    assert code("OTHER_MARKER") == "send_turn_not_answered"
    assert code("OTHER_MARKER", answered=True) is None
    assert code("SEND_MARKER", accepted=False) == "send_not_accepted"


def test_a_control_that_ends_at_its_target_has_made_its_observation_once_its_verdict_is_typed() -> None:
    """A no-op send or terminate is injected, so every later step fails against it. That failure is the control's
    evidence, not an abort: re-raising left the factory with no result to judge."""

    owners_alive = {"terminate_verdict": {"code": "terminate_left_owners_alive"}}
    assert control_made_its_observation("terminate", owners_alive) is True
    assert control_made_its_observation("send", {"send_turn_verdict": {"code": "send_accepted_without_a_turn"}}) is True
    # A production run with the same symptom is still the failure we want, and so is one under another control.
    assert control_made_its_observation(None, owners_alive) is False
    assert control_made_its_observation("send", owners_alive) is False
    # A steer or abort fault leaves the rest of the lifecycle able to run: a failure after it is a failure.
    assert control_made_its_observation("steer", {"steer_turn_verdict": {"code": "steer_delivered_as_queued_follow_up"}}) is False
    # Failing before the typed verdict, or with another code, means the control observed nothing.
    assert control_made_its_observation("terminate", {}) is False
    assert control_made_its_observation("terminate", {"terminate_verdict": {"code": "terminate_not_accepted"}}) is False


def test_a_control_that_ends_at_its_target_reports_the_target_and_its_preconditions() -> None:
    held = _healthy("terminate")
    # False only because the fault kept the owners alive, or because the step comes after the terminate.
    not_held_against_it = {"pi_helm_abort_native": False, "pi_helm_cold_resume_exact_file": False}

    assert control_run_assertions("terminate", "pi", {**held, **not_held_against_it}) == held
    assert control_run_assertions("send", "pi", {**held, **not_held_against_it}) == {
        "pi_helm_launch_registration": True,
        "pi_helm_native_tool_taxonomy": True,
        "pi_helm_send_idle": True,
    }
    # A steer or abort fault touches its target alone, and the run says so for every cell.
    assert control_run_assertions("abort", "pi", {**held, **not_held_against_it}) == {**held, **not_held_against_it}


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


_OWNERS_ALIVE = {"terminate_verdict": {"code": "terminate_left_owners_alive"}}


def test_terminate_control_does_not_require_evidence_the_fault_prevents() -> None:
    """OMP's launch_registration cannot be a terminate precondition.

    It is not a launch check: it ANDs settlement and a four-phase control
    identity that needs the cold_resume and final receipts. A no-op terminate
    keeps the session alive, so OMP can never reach that phase, and requiring
    it made the control inconclusive by construction -- fault fired, target
    rejected with the expected typed code, and still no pass (2026-09-19).

    The healthy prefix is every cell OMP has decided by the terminate.
    """
    # False only because the fault stops the run before settlement, or because the step follows the terminate.
    prevented = _healthy("terminate", "omp", launch_registration=False, cold_resume_exact_file=False)
    verdict = _control("terminate", provider="omp", assertions=prevented, observation=_OWNERS_ALIVE)
    assert verdict["preconditions_held"] is True, verdict
    assert verdict["status"] == "pass", verdict


@pytest.mark.parametrize(
    ("provider", "broken"),
    [
        ("omp", "steer_active"),
        # Decided before the terminate and, until the run reported them, free to be false.
        ("omp", "abort_native"),
        ("omp", "native_replacement_bound"),
        ("omp", "stale_owner_refused"),
        ("pi", "launch_registration"),
        ("pi", "native_tool_taxonomy"),
        ("pi", "reload_rebind"),
        ("pi", "session_replacement_rebind"),
        ("pi", "stale_owner_refused"),
    ],
)
def test_terminate_control_needs_every_cell_the_provider_decided_before_it(provider: str, broken: str) -> None:
    # A run in which an earlier step was already broken cannot attribute a terminate failure to the fault, so it
    # stays inconclusive rather than certifying the chip. And the broken cell is in what the run reports.
    assertions = _healthy("terminate", provider, **{broken: False})
    verdict = _control("terminate", provider=provider, assertions=assertions, observation=_OWNERS_ALIVE)
    assert verdict["preconditions_held"] is False, verdict
    assert verdict["status"] == "inconclusive", verdict
    assert control_run_assertions("terminate", provider, assertions)[f"{provider}_helm_{broken}"] is False


def test_terminate_preconditions_exclude_anything_gated_on_cleanup() -> None:
    """Pi's abort_native ANDs cleanup_ok, which a no-op terminate prevents.

    Requiring it cost Pi its Interrupt chip on 2026-09-19: fault fired, target
    rejected with the expected typed code, and inconclusive anyway because the
    run could not clean up while the fault kept the owners alive. Same defect
    as OMP's launch_registration, which ANDs settlement.
    """
    # False only because the fault keeps the owners alive.
    verdict = _control("terminate", assertions=_healthy("terminate", abort_native=False), observation=_OWNERS_ALIVE)
    assert verdict["preconditions_held"] is True, verdict
    assert verdict["status"] == "pass", verdict


def test_steer_at_a_tool_boundary_then_original_task_completed_is_model_noncompliance() -> None:
    entries = [
        *_task_prefix(),
        _entry("u2", "t1", "user", text=f"Stop and reply only with {STEER}"),
        _entry("a2", "u2", "assistant", text=f"{DONE}\n{STEER}", stop="stop"),
    ]
    verdict = steer_turn_verdict(entries, task_marker=TASK, steer_marker=STEER, task_done_marker=DONE)
    assert verdict["code"] == "original_task_completed_after_steer", verdict

    observation: dict = {"steer_turn_verdict": verdict}
    entry = pi_family_steer_noncompliance(verdict)
    assert entry is not None
    attach(observation, "pi_helm_steer_active", entry)

    assert observation["model_noncompliance"]["pi_helm_steer_active"] == {
        "reason": "steer_delivered_then_original_task_completed",
        "contract_evidence": {"steer_in_task_chain": True, "steer_adjacent_to_task_turn": True, "steer_at_tool_boundary": True},
    }


def test_queued_follow_up_attaches_no_noncompliance() -> None:
    entries = [
        *_task_prefix(),
        _entry("a1b", "t1", "assistant", text=f"{DONE}", stop="stop"),
        _entry("u2", "a1b", "user", text=f"Stop and reply only with {STEER}"),
        _entry("a2", "u2", "assistant", text=f"{DONE}\n{STEER}", stop="stop"),
    ]
    verdict = steer_turn_verdict(entries, task_marker=TASK, steer_marker=STEER, task_done_marker=DONE)
    assert verdict["code"] == "steer_delivered_as_queued_follow_up", verdict

    assert pi_family_steer_noncompliance(verdict) is None


# The waits a fault runs to their end, cut short here: the reply to a send that was dropped, owners that were never
# told to stop and the run they keep from being retired.
_FAULT_WAITS = {
    "send": {"_wait_native_marker": 6},
    "terminate": {
        "_wait_recorded_owners_dead": 2,
        "_wait_stopped": 2,
        "_record_retirement_claim_terminal": 2,
        "_wait_served_run_retirement": 2,
    },
}


# Decided in the run and not held against it. Pi's abort cell ANDs the cleanup a no-op terminate prevents. Its cold
# resume follows the terminate: against the fake the lifecycle gets that far, because the fake's launcher dies with
# the terminal the lifecycle closes, and the run ends at the second terminate.
_NOT_HELD_AGAINST = {("pi", "terminate"): {"pi_helm_abort_native", "pi_helm_cold_resume_exact_file"}}


def test_a_pi_send_control_run_with_a_false_launch_cell_reports_it_and_does_not_pass(tmp_path, monkeypatch, capsys):
    """The whole path for the control the review found unguarded: the producer's own verdict, and what it reports."""

    module = importlib.import_module("zerg.qa.pi_helm_lifecycle")
    cells = module.FAMILY.cells
    monkeypatch.setattr(
        module, "FAMILY", dataclasses.replace(module.FAMILY, cells=lambda seen: {**cells(seen), "pi_helm_launch_registration": False})
    )
    monkeypatch.delenv(fake.FAULT_ENV, raising=False)
    wait = helm._wait_native_marker
    monkeypatch.setattr(
        helm, "_wait_native_marker", lambda *a, **k: wait(*a, **{**k, "timeout": _FAULT_WAITS["send"]["_wait_native_marker"]})
    )
    code, root, _left = fake.run_lifecycle("pi", tmp_path, monkeypatch.setenv, arguments=("--negative-control", "send"))
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    verdict = result["negative_control"]
    assert (verdict["fault_fired"], verdict["target_rejected"], verdict["preconditions_held"]) == (True, True, False)
    assert (code, result["status"], verdict["status"]) == (1, "fail", "inconclusive")
    assert result["assertions"] == {
        "pi_helm_launch_registration": False,
        "pi_helm_native_tool_taxonomy": True,
        "pi_helm_send_idle": False,
    }


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("dies_after", "fired"),
    [
        # After the steered turn: the launcher had held the steer back, and left its receipt.
        (6, True),
        # After the idle send's turn, before any steer was sent.
        (2, False),
    ],
)
def test_a_control_whose_run_died_before_a_verdict_says_whether_its_fault_had_fired(dies_after, fired, tmp_path, monkeypatch, capsys):
    """OMP's steer control, and the launcher dies. The run failed, so the control decided nothing; the result says
    that, and what is known: whether the fault had fired by then, and that nothing evaluated the preconditions.

    A run that raised used to leave no control block here, and a bare one from two other producers, and the factory
    read what was missing as "the fault did not fire and a precondition did not hold".
    """

    monkeypatch.setenv(fake.FAULT_ENV, f"exit_after:{dies_after}")
    code, root, _left = fake.run_lifecycle("omp", tmp_path, monkeypatch.setenv, arguments=("--negative-control", "steer"))
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    assert (code, result["status"], result["failure_code"]) == (1, "fail", "omp_helm_lifecycle_failed")
    control = result["negative_control"]
    assert (control["control"], control["status"], control["run_failed"]) == ("steer", "inconclusive", True)
    assert control["preconditions_held"] is None
    assert control["fault_fired"] is fired
    assert [receipt["fault"] for receipt in control["fault_receipts"]] == (["omp_steer_as_follow_up"] if fired else [])
    # What the run had reached is still there: the steer cell it had decided, false under the fault, or no verdict on it.
    assert result.get("assertions", {}).get("omp_helm_steer_active") is (False if fired else None)


@pytest.mark.timeout(120)
@pytest.mark.parametrize("control", sorted(ENDS_AT_TARGET))
@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_control_that_ends_at_its_target_decides(provider, control, tmp_path, monkeypatch, capsys):
    """Run as the factory runs it, against a launcher that does what the faulted engine does and leaves its receipt.

    The control passes, for the code it expects, and its summary lets the factory certify it: the factory refuses a
    control whose run reports any false cell but the target (control-plane ``negative_controls.normalize_verdict``),
    and a run that ends at its target has cells it never decided.

    These runs are in this file and not beside the lifecycle fixture's because of how the suite is scheduled
    (``run_backend_tests_lite.sh``): that file is the run's longest, and a file of four long tests would run last.
    """

    monkeypatch.delenv(fake.FAULT_ENV, raising=False)
    for name, seconds in _FAULT_WAITS[control].items():
        monkeypatch.setattr(
            helm, name, lambda *a, _wait=getattr(helm, name), _seconds=seconds, **k: _wait(*a, **{**k, "timeout": _seconds})
        )
    code, root, _left = fake.run_lifecycle(provider, tmp_path, monkeypatch.setenv, arguments=("--negative-control", control))
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    _fault, target, verdict_key, expected = NEGATIVE_CONTROLS[control]
    verdict = result["negative_control"]
    assert (code, result["status"], verdict["status"]) == (0, "pass", "pass"), verdict
    assert verdict["fault_fired"] and verdict["preconditions_held"] and verdict["target_rejected"]
    assert verdict["observed_code"] == result["observation"][verdict_key]["code"] == expected
    assert [cell for cell, held in result["assertions"].items() if not held] == [f"{provider}_{target}"]
    # It reports the target and what it was held to, which is every cell the run had decided by then: a decided
    # cell it was not held to could be false and the control still certify, with nothing to show the factory.
    preconditions = set(control_preconditions(control, provider))
    assert set(result["assertions"]) == {f"{provider}_{target}", *preconditions}
    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")
    observation = result["observation"]
    decided = set(reached_only(module.FAMILY.cells(observation), observation, module.FAMILY.reached))
    assert decided - {f"{provider}_{target}"} - _NOT_HELD_AGAINST.get((provider, control), set()) == preconditions
    # The failure that ended the run is kept as the control's evidence, not as the run's.
    assert result["observation"]["failure_after_control_verdict"] and "error" not in result
    # The receipt is the launcher's, for this session and this fault.
    receipts = map(json.loads, (root / "qa-fault-receipt.jsonl").read_text(encoding="utf-8").splitlines())
    assert {(line["fault"], line["session_id"]) for line in receipts} == {(verdict["fault"], result["observation"]["session_id"])}
