"""The Antigravity Helm lifecycle producer: its fixture, its oracles and its registration.

The live run (a real agy TUI behind the scripted Gemini endpoint) is a bench receipt; these tests hold what does not need
a provider: that the scripted fixture answers each step's request with the turn that step expects, that each oracle
rejects the run that did not do what its cell says, and that the registration covers the schema's cells.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from zerg.qa import antigravity_helm_lifecycle as helm
from zerg.qa import antigravity_scripted_lane as lane

from tests_lite.test_antigravity_scripted_lane import TOOLS
from tests_lite.test_antigravity_scripted_lane import _call
from tests_lite.test_antigravity_scripted_lane import _model
from tests_lite.test_antigravity_scripted_lane import _plan
from tests_lite.test_antigravity_scripted_lane import _result
from tests_lite.test_antigravity_scripted_lane import _user

TAG = uuid4().hex


def _marker(kind: str) -> str:
    return f"{lane.HELM_PREFIX}_{kind}_{uuid4().hex}"


def test_the_helm_fixture_answers_every_step_with_its_own_turn() -> None:
    turns = lane.helm_script(TAG)["turns"]
    launch, idle, send_task, sent, first, steered, interrupt, recover = (
        _marker(k) for k in ("LAUNCH", "IDLE", "SENDTASK", "SENT", "STEER_FIRST", "STEERED", "INTERRUPT", "RECOVER")
    )
    history = [_user(f"Reply with exactly {launch}"), _model(launch)]
    assert _plan(turns, history).turn_id == "launch"
    # An idle send is a turn of its own, answered with its own marker.
    history += [_user(f"Reply with exactly {idle}")]
    assert _plan(turns, history).turn_id == "idle"
    assert _plan(turns, history).text == idle
    history += [_model(idle)]
    # A send: a slow tool, then the tool result alone (no send landed) or with the remote message beside it.
    history += [_user(f"Do the first slow task: {send_task}")]
    assert _plan(turns, history).tool_call is not None
    history += [_call("sleep 15"), _result(lane.helm_send_tool_output(TAG))]
    assert _plan(turns, history).turn_id == "send-tool-result"
    landed = history + [_user(f"Change of plan, reply with exactly {sent}")]
    assert _plan(turns, landed).turn_id == "sent"
    assert _plan(turns, landed).text == sent
    history = landed + [_model(sent), _user(f"Do the slow task: {first}")]
    assert _plan(turns, history).tool_call is not None
    tool_output = lane.helm_steer_tool_output(TAG)
    history += [_call("sleep 15"), _result(tool_output)]
    # No steer landed: the tool result alone is answered with the first prompt's marker.
    assert _plan(turns, history).turn_id == "steer-tool-result"
    # A steer that landed rides the same request as the tool result.
    landed = history + [_user(f"Change of plan, reply with exactly {steered}")]
    assert _plan(turns, landed).turn_id == "steered"
    assert _plan(turns, landed).text == steered
    history = landed + [_model(steered), _user(f"Do the interrupted task: {interrupt}")]
    assert _plan(turns, history).turn_id == "interrupt"
    # The interrupted tool's output reaching the model is a served turn of its own, which the oracle counts as a leak.
    leaked = history + [_call("sleep 8"), _result(lane.HELM_INTERRUPT_TOOL_OUTPUT)]
    assert _plan(turns, leaked).turn_id == "interrupt-tool-result"
    recovered = history + [_model("x"), _user(f"Reply with exactly {recover}")]
    assert _plan(turns, recovered).turn_id == "recover"


def test_every_helm_turn_the_receipt_expects_exists_in_the_fixture() -> None:
    ids = {turn["id"] for turn in lane.helm_script(TAG)["turns"]}
    assert lane.HELM_EXPECTED_TURNS <= ids


def _claim(intent: str, text: str, event: str = "PreInvocation") -> dict:
    return {"intent": intent, "text": text, "hook_event": event}


def test_a_steer_that_landed_in_the_turn_passes() -> None:
    steered, output = _marker("STEERED"), lane.helm_steer_tool_output(TAG)
    verdict = helm.steer_landed(
        claims=[_claim("steer", f"Change of plan, reply with exactly {steered}")],
        requests=[f"first {output}", f"{steered} and {output}"],
        steer_marker=steered,
        tool_output=output,
        answered=True,
    )
    assert verdict["passed"] is True


def test_a_steer_that_became_its_own_turn_does_not_pass() -> None:
    steered, output = _marker("STEERED"), lane.helm_steer_tool_output(TAG)
    verdict = helm.steer_landed(
        claims=[_claim("steer", steered)],
        # The steer text arrives in a request that never carried the slow tool's output: a later turn, not the running one.
        requests=[f"first {output}", f"only {steered}"],
        steer_marker=steered,
        tool_output=output,
        answered=True,
    )
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "steer_did_not_change_course"


def test_an_unclaimed_steer_does_not_pass() -> None:
    steered, output = _marker("STEERED"), lane.helm_steer_tool_output(TAG)
    verdict = helm.steer_landed(claims=[], requests=[f"{steered} {output}"], steer_marker=steered, tool_output=output, answered=True)
    assert verdict["failure_code"] == "steer_not_claimed"


def test_a_send_that_joined_the_running_turn_passes() -> None:
    sent, output = _marker("SENT"), lane.helm_send_tool_output(TAG)
    verdict = helm.send_delivered(
        claims=[_claim("send", f"Change of plan, reply with exactly {sent}")],
        requests=[f"first {output}", f"{sent} and {output}"],
        marker=sent,
        tool_output=output,
        answered=True,
        error=None,
    )
    assert verdict["passed"] is True


def _idle_verdict(**overrides) -> dict:
    marker = _marker("IDLE")
    arguments = {
        "claims": [{**_claim("send", f"Reply with exactly {marker}", "IdleWake"), "claimed_by": "launcher"}],
        "requests": [f"history {marker}"],
        "marker": marker,
        "answered": True,
        "error": None,
        "idle_at_send": True,
    }
    arguments.update(overrides)
    return helm.send_idle_delivered(**arguments)


def test_a_send_to_an_idle_session_that_started_a_turn_passes() -> None:
    verdict = _idle_verdict()
    assert verdict["passed"] is True
    assert verdict["claimed_by"] == "launcher"


def test_an_idle_send_that_timed_out_does_not_pass() -> None:
    verdict = _idle_verdict(claims=[], requests=[], answered=False, error='HTTP 503: {"detail":"Request timed out"}')
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "idle_send_rejected"


def test_an_idle_send_that_was_never_claimed_does_not_pass() -> None:
    assert _idle_verdict(claims=[])["failure_code"] == "idle_send_not_claimed"


def test_an_idle_send_the_model_never_received_does_not_pass() -> None:
    assert _idle_verdict(requests=["nothing"])["failure_code"] == "idle_send_never_reached_the_model"


def test_an_idle_send_with_no_archived_reply_does_not_pass() -> None:
    assert _idle_verdict(answered=False)["failure_code"] == "idle_send_reply_missing"


def test_a_send_to_a_session_that_was_not_idle_does_not_count_as_an_idle_send() -> None:
    verdict = _idle_verdict(idle_at_send=False)
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "session_not_idle"


def test_a_terminate_that_ended_the_group_and_the_served_run_passes() -> None:
    verdict = helm.terminate_held(dispatched=True, provider_dead=True, group_dead=True, run_lifecycle="ended")
    assert verdict["passed"] is True


def test_a_terminate_that_left_the_served_run_running_does_not_pass() -> None:
    verdict = helm.terminate_held(dispatched=True, provider_dead=True, group_dead=True, run_lifecycle="running")
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "served_run_still_running_after_terminate"


def test_a_terminate_that_left_the_group_alive_does_not_pass() -> None:
    verdict = helm.terminate_held(dispatched=True, provider_dead=True, group_dead=False, run_lifecycle="ended")
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "terminate_did_not_stop_the_group"


def test_a_send_to_an_idle_session_that_timed_out_does_not_pass() -> None:
    sent, output = _marker("SENT"), lane.helm_send_tool_output(TAG)
    verdict = helm.send_delivered(
        claims=[], requests=[], marker=sent, tool_output=output, answered=False, error='HTTP 503: {"detail":"Request timed out"}'
    )
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "send_rejected"


def test_a_send_claimed_but_never_in_the_turn_does_not_pass() -> None:
    sent, output = _marker("SENT"), lane.helm_send_tool_output(TAG)
    verdict = helm.send_delivered(
        claims=[_claim("send", sent)], requests=[f"only {sent}"], marker=sent, tool_output=output, answered=True, error=None
    )
    assert verdict["failure_code"] == "send_did_not_reach_the_turn"


def test_an_interrupt_that_stopped_the_next_model_call_passes() -> None:
    verdict = helm.interrupt_held(
        claims=[_claim("interrupt", "interrupt", "PostInvocation")],
        requests=["a request without the tool output"],
        tool_output=lane.HELM_INTERRUPT_TOOL_OUTPUT,
        tui_alive=True,
        recovered=True,
    )
    assert verdict["passed"] is True


def test_an_interrupt_that_let_the_model_see_the_tool_output_does_not_pass() -> None:
    verdict = helm.interrupt_held(
        claims=[_claim("interrupt", "interrupt")],
        requests=[f"call {lane.HELM_INTERRUPT_TOOL_OUTPUT}"],
        tool_output=lane.HELM_INTERRUPT_TOOL_OUTPUT,
        tui_alive=True,
        recovered=True,
    )
    assert verdict["passed"] is False
    assert verdict["failure_code"] == "model_called_after_interrupt"


def test_an_interrupt_that_killed_the_tui_does_not_pass() -> None:
    verdict = helm.interrupt_held(claims=[_claim("interrupt", "interrupt")], requests=[], tool_output="x", tui_alive=False, recovered=False)
    assert verdict["passed"] is False


def test_the_cells_follow_the_lifecycle_report() -> None:
    report = {
        "status": "passed",
        "lifecycle": {
            "launch_registration": {"a": True, "b": True},
            "send_idle": {"passed": True},
            "send_active": {"passed": True},
            "steer_active": {"passed": True},
            "interrupt": {"passed": True},
            "terminate_owned": {"passed": True},
        },
    }
    assert all(helm.lifecycle_assertions(report, cleanup_ok=True).values())
    # A terminate that is not proven leaves the cleanup-bound cell false and the earlier ones standing.
    failed = {**report, "status": "failed", "lifecycle": {**report["lifecycle"], "terminate_owned": {"passed": False}}}
    cells = helm.lifecycle_assertions(failed, cleanup_ok=True)
    assert cells["antigravity_helm_terminate_owned"] is False
    assert cells["antigravity_helm_steer_active"] is True
    # A cell with no observation reads false, never true.
    assert helm.lifecycle_assertions({"lifecycle": {}}, cleanup_ok=True)["antigravity_helm_send_active"] is False


def test_the_registration_names_every_cell_it_judges() -> None:
    assert {cell for cell, _variant in helm.REGISTRATION.assertion_cells} == set(helm.ASSERTIONS)
    assert helm.REGISTRATION.evidence_classes == ("scripted_model",)
    assert helm.REGISTRATION.credential_binding_ids_by_evidence_class == {"scripted_model": ("runtime_host_control",)}
    assert Path(helm.REGISTRATION.implementation).name == "antigravity_helm_lifecycle.py"


def test_a_later_turns_request_carrying_the_finished_task_output_is_not_a_leak() -> None:
    marker = _marker("RECOVER")
    verdict = helm.interrupt_held(
        claims=[_claim("interrupt", "interrupt")],
        requests=[f"history {lane.HELM_INTERRUPT_TOOL_OUTPUT} {marker}"],
        tool_output=lane.HELM_INTERRUPT_TOOL_OUTPUT,
        tui_alive=True,
        recovered=True,
        later_marker=marker,
    )
    assert verdict["passed"] is True
