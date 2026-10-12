"""The Pi-family Helm producers, end to end through their real ``main``, against a fake Longhouse.

The fake (``_pi_family_helm_fake``) stands at the producers' process boundary: the ``longhouse`` and
``longhouse-engine`` executables and the Runtime Host. Its docstring says what a green run does not cover.
"""

from __future__ import annotations

import copy
import importlib
import json
import time
from pathlib import Path

import pytest

from tests_lite import _pi_family_helm_fake as fake
from tests_lite._factory_envelope import assert_result_conforms
from zerg.qa import omp_background_producer
from zerg.qa.openrouter_routing import OPENROUTER_QUALIFICATION_ROUTING

# A whole lifecycle of real waits and process exits; the suite's 60 s guard is for a hang, not for this.
pytestmark = pytest.mark.timeout(240)

_REPO = Path(__file__).resolve().parents[2]
_TOKEN = "fixture-agents-token"


@pytest.fixture
def longhouse(tmp_path, monkeypatch):
    monkeypatch.delenv(fake.FAULT_ENV, raising=False)
    with fake.longhouse(tmp_path, monkeypatch.setenv) as made:
        yield made


@pytest.fixture(scope="module")
def whole(tmp_path_factory):
    """Each provider's whole lifecycle, run once: its exit code, its result, its evidence root, and what it left isolated."""

    runs: dict[str, tuple[int, dict, Path, list[Path]]] = {}

    def run(provider: str) -> tuple[int, dict, Path, list[Path]]:
        if provider not in runs:
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.delenv(fake.FAULT_ENV, raising=False)
                # The first control request through the Runtime Host is refused once as a reconnecting channel.
                code, root, left = fake.run_lifecycle(
                    provider, tmp_path_factory.mktemp(provider), monkeypatch.setenv, reconnecting=1, page_size=5
                )
            runs[provider] = (code, json.loads((root / "result.json").read_text(encoding="utf-8")), root, left)
        return runs[provider]

    return run


def _run(provider: str, cli: Path, tmp_path: Path, monkeypatch) -> tuple[int, dict, Path]:
    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")
    monkeypatch.setenv(module.FAMILY.live_env, "1")
    root = tmp_path / "evidence"
    code = module.main(fake.producer_arguments(provider, cli, root))
    return code, json.loads((root / "result.json").read_text(encoding="utf-8")), root


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_whole_lifecycle_holds_every_cell(provider, whole):
    code, result, root, left_isolated = whole(provider)
    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")

    not_held = [name for name, held in result.get("assertions", {}).items() if not held]
    assert not not_held and result.get("assertions"), {"not held": not_held, "error": result.get("error")}
    assert (code, result["status"]) == (0, "pass")
    _conforms(provider, result)
    assert set(result["assertions"]) == set(module.ASSERTIONS)
    assert json.loads((root / "cleanup-receipt.json").read_text(encoding="utf-8"))["status"] == "pass"
    # The producer removed its own isolation; the fixture had nothing to remove.
    assert left_isolated == []
    # The one refusal the first host request waited out is in that control's record, and nowhere else.
    retries = {
        name: record["command"]["retries"]["count"]
        for name, record in result["observation"]["controls"].items()
        if isinstance(record.get("command"), dict) and "retries" in record["command"]
    }
    assert retries["send"] == 1 and sum(retries.values()) == 1
    assert result["observation"]["controls"]["send"]["command"]["retries"]["refused_with"]["http_status"] == 409
    # The host served its events five to a page: each served turn was read to the end of them, under one generation.
    pages = {turn: served["served_projection"]["events_page"] for turn, served in result["observation"]["convergence"].items()}
    assert all(page["complete"] is True and page["returned_count"] == page["total"] for page in pages.values())
    assert pages["resume"]["pages_read"] > 2 and pages["launch"]["pages_read"] >= 1
    # The refused stale frame carried text of this run's own, and none of it is in what the provider wrote.
    frame_text = result["observation"]["controls"]["stale_owner"]["frame_text"]
    assert f"{module.FAMILY.marker_prefix}_STALE_" in frame_text
    retained = [root / item["retained_source_path"] for item in result["observation"]["sources"]]
    assert retained and not any(frame_text.encode() in path.read_bytes() for path in retained)
    if module.FAMILY.reached is not None:
        # Every record a cell's step is proven reached by is one the run wrote.
        assert set(module.FAMILY.reached) == set(module.ASSERTIONS)
        assert module.reached_assertions(result["observation"]) == result["assertions"]


# One fact of a whole lifecycle's record, broken: the cell that rests on it no longer holds. The record is the
# real one, so a cell that stopped reading a fact, or reads it from where the run does not write it, shows here.
_BROKEN = [
    ("omp", "launch_registration", "control_identity.replacement.control_subject_key", "connection:another:lease"),
    ("omp", "launch_registration", "control_identity", {}),
    ("omp", "launch_registration", "controls.terminate.command.transport", "local_engine_command"),
    ("omp", "launch_registration", "convergence.resume.status", "unproven"),
    ("omp", "launch_registration", "shipper.started", False),
    ("omp", "send_idle", "controls.send.evidence.marker_count", 2),
    ("omp", "send_idle", "controls.send.command.payload.client_request_id", "another-request"),
    ("omp", "follow_up_native", "controls.follow_up.queued_not_sent", False),
    ("omp", "follow_up_native", "controls.follow_up.active_state.phase", "idle"),
    ("omp", "steer_active", "steer_turn_verdict.passed", False),
    ("omp", "steer_active", "controls.steer.active_state.native_session_id", "another-session"),
    ("omp", "abort_native", "controls.abort.evidence.terminal", False),
    ("omp", "abort_native", "abort_turn_verdict.after_send_accepted", False),
    ("omp", "terminate_owned", "cleanup.served_run_retired", False),
    ("omp", "terminate_owned", "settlement.agent_end_terminal", False),
    ("omp", "cold_resume_exact_file", "controls.cold_resume.context_evidence.marker_count", 2),
    ("omp", "cold_resume_exact_file", "controls.cold_resume.state.session_file", "/another/session.jsonl"),
    ("omp", "cold_resume_exact_file", "controls.cold_resume.terminal_evidence.channel_source_bound", False),
    ("omp", "stale_owner_refused", "controls.stale_owner.error_code", "bad_request"),
    ("omp", "stale_owner_refused", "controls.stale_owner.second_launcher.owner_refusal_observed", False),
    ("omp", "native_replacement_bound", "controls.replacement.evidence.native_source_bound", False),
    # Every turn is shown served, and each cell rests on its own turn's.
    ("omp", "send_idle", "convergence.send.status", "unproven"),
    ("omp", "follow_up_native", "convergence.follow_up.status", "unproven"),
    ("omp", "steer_active", "convergence.steer.status", "unproven"),
    ("omp", "native_replacement_bound", "convergence.replacement.status", "unproven"),
    ("omp", "cold_resume_exact_file", "convergence.resume_context.status", "unproven"),
    ("pi", "launch_registration", "convergence.launch.served_controls", False),
    ("pi", "launch_registration", "shipper.machine_name", ""),
    # Control is held to the owner's lease when the owner starts, not only after a rebind.
    ("pi", "launch_registration", "control_identity.initial.control_subject_key", "connection:another:lease"),
    ("pi", "launch_registration", "control_identity.initial.actions.send_input", "unavailable"),
    ("pi", "cold_resume_exact_file", "control_identity.cold_resume", {}),
    ("pi", "cold_resume_exact_file", "control_identity.final.owner_identity", ["another", "owner"]),
    ("pi", "native_tool_taxonomy", "native_taxonomy.tool_calls_without_results", ["call-without-result"]),
    ("pi", "send_idle", "controls.send.command.accepted", False),
    # Each control goes the way a remote user's does, and once more through the engine's local command: neither
    # path stands in for the other.
    ("pi", "send_idle", "controls.send.command.transport", "local_engine_command"),
    ("pi", "send_idle", "controls.local_send.command.transport", "runtime_host_agents_api"),
    ("pi", "send_idle", "controls.local_send.evidence.assistant_answer_rows", 0),
    ("pi", "steer_active", "controls.steer.command.transport", "local_engine_command"),
    ("pi", "steer_active", "controls.local_steer.command.accepted", False),
    ("pi", "steer_active", "local_steer_turn_verdict.passed", False),
    ("pi", "abort_native", "controls.abort.command.transport", "local_engine_command"),
    ("pi", "abort_native", "controls.local_abort.evidence.observed", False),
    ("pi", "abort_native", "local_abort_turn_verdict.after_send_accepted", False),
    ("pi", "terminate_owned", "controls.terminate.command.transport", "local_engine_command"),
    ("pi", "terminate_owned", "controls.final_terminate.command.transport", "runtime_host_agents_api"),
    ("pi", "send_idle", "flush.send.status", "fail"),
    ("pi", "send_idle", "convergence.send.binding.one_thread", False),
    # No busy turn was seen before the steer, or before the send that was to be queued behind one.
    ("pi", "steer_active", "controls.steer.active_state", {}),
    ("pi", "follow_up_native", "controls.follow_up.active_state", {}),
    ("pi", "follow_up_native", "controls.follow_up.queued_not_sent", False),
    ("pi", "follow_up_native", "controls.follow_up.evidence.user_marker_rows", 2),
    ("pi", "abort_native", "controls.abort.evidence.observed", False),
    ("pi", "abort_native", "controls.abort.evidence.retained_source_path", None),
    # Cleanup is a gate no record of the turn can stand in for.
    ("pi", "abort_native", "cleanup.status", "fail"),
    ("pi", "abort_native", "cleanup.cleanup_errors", ["shipper stop failed"]),
    ("pi", "terminate_owned", "cleanup.isolation_removed", False),
    ("pi", "terminate_owned", "controls.final_terminate.stopped_state.status", "owners_alive_at_deadline"),
    ("pi", "reload_rebind", "controls.reload.send.accepted", False),
    ("pi", "reload_rebind", "convergence.reload.served_controls", False),
    ("pi", "session_replacement_rebind", "controls.replacement.evidence.native_header_session_id", "another-session"),
    ("pi", "session_replacement_rebind", "convergence.replacement.binding.one_session", False),
    # The phrase reached the resumed turn in a prompt, or was given twice, or its recall is not served as the session's.
    ("pi", "cold_resume_exact_file", "controls.cold_resume.context_evidence.user_marker_rows", 1),
    ("pi", "cold_resume_exact_file", "controls.replacement.context_evidence.user_marker_rows", 2),
    ("pi", "cold_resume_exact_file", "convergence.resume_context.binding.provider_session_bound", False),
    ("pi", "stale_owner_refused", "controls.stale_owner.native_dispatch.observed", True),
    ("pi", "stale_owner_refused", "controls.stale_owner.error_code", "bad_request"),
    ("pi", "stale_owner_refused", "controls.stale_owner.second_launcher.owner_refusal_observed", False),
    ("pi", "stale_owner_refused", "controls.stale_owner.second_launcher", None),
]


@pytest.mark.parametrize(("provider", "cell", "fact", "broken"), _BROKEN)
def test_a_cell_does_not_hold_once_a_fact_it_rests_on_is_broken(provider, cell, fact, broken, whole):
    observation = copy.deepcopy(whole(provider)[1]["observation"])
    *parents, leaf = fact.split(".")
    record = observation
    for name in parents:
        record = record[name]
    assert leaf in record and record[leaf] != broken, f"the run does not record {fact}"
    record[leaf] = broken
    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")
    assert module.FAMILY.cells(observation)[f"{provider}_helm_{cell}"] is False


def _conforms(provider: str, result: dict, cell: str = "launch_registration") -> None:
    """The factory would take ``result`` as the answer to its command for ``cell``."""

    module = importlib.import_module(f"zerg.qa.{provider}_helm_lifecycle")
    variant = module.PRODUCER.variants[module.ASSERTIONS.index(f"{provider}_helm_{cell}")]
    assert_result_conforms(module, result, variant=variant)


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_provider_that_dies_mid_run_is_a_typed_failure_with_what_it_reached_kept(provider, longhouse, tmp_path, monkeypatch, capsys):
    cli, _isolation = longhouse
    # The launcher and its provider die, with no terminal state written, once the idle send has been answered.
    monkeypatch.setenv(fake.FAULT_ENV, "exit_after:2")
    code, result, root = _run(provider, cli, tmp_path, monkeypatch)
    capsys.readouterr()

    _conforms(provider, result)
    assert (code, result["status"], result["failure_code"]) == (1, "fail", f"{provider}_helm_lifecycle_failed")
    assert "Runtime Host HTTP 502" in result["error"]
    # Every cell it reached had held, so the run reports no verdict: it is a failure of the run, not of a cell.
    assert "observation" not in result and "assertions" not in result
    cleanup = json.loads((root / "cleanup-receipt.json").read_text(encoding="utf-8"))
    assert cleanup["status"] == "pass"
    # What it had reached stays as evidence: the send that held, and nothing for the follow-up that never ran.
    # OMP's failed run keeps it as the observation it had; Pi's reports no verdict and keeps it as diagnostics.
    reached = result["partial_observation" if provider == "omp" else "diagnostic_observation"]
    assert reached["controls"]["send"]["marker_wait"]["status"] == "answered" and "follow_up" not in reached["controls"]
    assert reached["cleanup"] == cleanup
    assert (root / "partial-observation.json").is_file()
    if provider == "pi":
        # The launch had held. The idle send needs its second half, through the local command, which the run never reached.
        assert result["diagnostic_assertions"]["pi_helm_launch_registration"] is True
        assert result["diagnostic_assertions"]["pi_helm_send_idle"] is False and "local_send" not in reached["controls"]
        routing = json.loads((root / "openrouter-routing-receipt.json").read_text(encoding="utf-8"))
        assert (routing["model_id"], routing["routing"]) == ("fake-model", OPENROUTER_QUALIFICATION_ROUTING)


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_events_that_cannot_be_read_to_their_end_fail_the_run_and_are_not_taken_as_a_page(provider, tmp_path, monkeypatch, capsys):
    # The host says there are more events and gives no cursor to read them with.
    code, root, _left = fake.run_lifecycle(provider, tmp_path, monkeypatch.setenv, pages_never_end=True)
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    assert (code, result["status"], result["failure_code"]) == (1, "fail", f"{provider}_helm_lifecycle_failed")
    assert "Runtime Host returned a non-exhaustible events page" in result["error"]
    assert "assertions" not in result


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_failing_verdict_reached_before_a_late_failure_is_kept(provider, longhouse, tmp_path, monkeypatch, capsys):
    cli, _isolation = longhouse
    # The steer is held until its turn is over, which the steer cell must refuse; then the provider dies.
    monkeypatch.setenv(fake.FAULT_ENV, "steer_as_follow_up,exit_after:6")
    code, result, _root = _run(provider, cli, tmp_path, monkeypatch)
    capsys.readouterr()

    _conforms(provider, result, cell="steer_active")
    assert (code, result["status"], result["failure_code"]) == (1, "fail", f"{provider}_helm_lifecycle_failed")
    steer = f"{provider}_helm_steer_active"
    if provider == "omp":
        # The verdicts of the steps it reached, the failing one among them; none for a step it did not reach.
        assert result["assertions"] == {"omp_helm_send_idle": True, "omp_helm_follow_up_native": True, steer: False}
        assert result["observation"]["steer_turn_verdict"]["code"] == "steer_delivered_as_queued_follow_up"
    else:
        # Pi reports no verdict of a failed run: what it had decided is diagnostic only.
        assert "assertions" not in result
        assert result["diagnostic_assertions"][steer] is False
        assert result["diagnostic_observation"]["steer_turn_verdict"]["code"] == "steer_delivered_as_queued_follow_up"


@pytest.mark.parametrize(
    ("dies_after", "refused", "recorded"),
    [
        # After the steered turn: the idle send through the local command is the next thing sent.
        (5, "Pi Helm local_send was not accepted, so it started no turn", "refused"),
        # After that send's turn: the send that was to start the busy turn for the local steer.
        (6, "Pi Helm local_steer turn's send was not accepted, so it started no turn", "answered"),
    ],
)
def test_a_refused_send_ends_the_run_where_it_is_refused(dies_after, refused, recorded, longhouse, tmp_path, monkeypatch, capsys):
    cli, _isolation = longhouse
    # The launcher is gone, so the engine's local command has no session to send to and says so at once.
    monkeypatch.setenv(fake.FAULT_ENV, f"exit_after:{dies_after}")
    started = time.monotonic()
    code, result, _root = _run("pi", cli, tmp_path, monkeypatch)
    capsys.readouterr()

    assert (code, result["status"], result["failure_code"]) == (1, "fail", "pi_helm_lifecycle_failed")
    assert refused in result["error"]
    controls = result["diagnostic_observation"]["controls"]
    assert controls["local_send"]["marker_wait"]["status"] == recorded and "local_steer" not in controls
    # Not at the end of a wait for a reply or a busy state that could never come.
    assert time.monotonic() - started < 45


@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_run_that_fails_only_in_its_cleanup_keeps_everything_it_recorded(provider, tmp_path, monkeypatch, capsys):
    # Every step holds, and the session cannot be shown retired afterwards.
    code, root, _left = fake.run_lifecycle(provider, tmp_path, monkeypatch.setenv, never_hides=True)
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    cleanup = json.loads((root / "cleanup-receipt.json").read_text(encoding="utf-8"))
    assert (code, result["status"], cleanup["status"], cleanup["canary_session_hidden"]) == (1, "fail", "fail", False)
    if provider == "omp":
        # OMP's cells that rest on cleanup do not hold: a verdict, with the whole observation in the result.
        not_held = {name for name, held in result["assertions"].items() if not held}
        assert not_held == {"omp_helm_terminate_owned"} and "controls" in result["observation"]
    else:
        # Pi ends the run: a typed failure that keeps the whole record, in the result and as a file of its own.
        assert result["failure_code"] == "pi_helm_lifecycle_failed" and "assertions" not in result
        kept = json.loads((root / "partial-observation.json").read_text(encoding="utf-8"))["observation"]
        assert set(kept["controls"]) == set(result["diagnostic_observation"]["controls"]) >= {"launch", "cold_resume", "final_terminate"}
        assert kept["cleanup"]["status"] == "fail"


def test_the_background_scope_ends_after_its_first_turn(longhouse, tmp_path, monkeypatch, capsys):
    """The OMP background scenario rides this lifecycle for one turn: none of the Helm controls run."""

    cli, _isolation = longhouse
    module = importlib.import_module("zerg.qa.omp_helm_lifecycle")
    monkeypatch.setenv("LONGHOUSE_OMP_LIVE", "1")
    args = module.PRODUCER.parser().parse_args(fake.producer_arguments("omp", cli, tmp_path / "evidence"))
    args.background_prompt = "Start one background job and wait for it."
    result = module.run_omp_helm(args)
    capsys.readouterr()

    assert result["observation"]["scenario_scope"] == "background"
    assert set(result["observation"]["controls"]) == {"launch"}
    assert "error" not in result and result["observation_scope"] == "scenario"
    assert result["observation"]["cleanup"]["status"] == "pass"
    # What the background producer asks of the lifecycle it rode on: launch, shipper, convergence, archive, cleanup.
    assert omp_background_producer.background_run_healthy({"provider": "omp", "observation_scope": "scenario", **result})
