#!/usr/bin/env python3
"""Continuous producer for claude/session.activity.turn_boundary.

Proves ``activity_returns_to_quiescent_at_turn_boundary``: after a real
managed Claude turn finishes, the Runtime Host's canonical per-session
activity fact settles to ``quiescent`` -- not merely that the assistant's
reply text eventually shows up in the transcript.

``cursor_helm_product_e2e.py``'s ``settled()`` helper documents the exact bug
class this guards against: transcript arrival and the served activity axis
are independent facts, so a finished turn can keep reading as ``thinking``
(or silently decay to ``unknown`` once its freshness TTL expires) even though
the model is done and the words are on screen. This producer polls the real
served fact, starting immediately once the turn's response is observed and
*before* the session is closed, so a session-close side effect can never
launder a real "stuck non-quiescent" bug into a trivial pass.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from zerg.qa.claude_live_session_support import await_assistant_marker
from zerg.qa.claude_live_session_support import claude_launch_environment
from zerg.qa.claude_live_session_support import close_session
from zerg.qa.claude_live_session_support import failed_session_close_receipt
from zerg.qa.claude_live_session_support import isolation_paths
from zerg.qa.claude_live_session_support import launch_claude_session
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.claude_live_session_support import start_machine_and_shipper
from zerg.qa.claude_live_session_support import wait_for_served_quiescent
from zerg.qa.claude_live_session_support import write_claude_cleanup_aggregate
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.live_session_toolkit import prepare_claude_profile
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

_SCENARIO_ID = "claude_turn_boundary_quiescent"
_ASSERTION_ID = "activity_returns_to_quiescent_at_turn_boundary"
# schemas/managed_providers.yml's session.activity.turn_boundary entry for
# claude carries no `variant:` key, so the compiler's execution_variant is
# always this generated `cell:<provider>:<assertion_id>:<scenario_id>` form
# (see resume_assurance.execution_variant_key / _producer_supports_cell). That
# is what the real sandbox passes as --variant -- see
# codex_turn_boundary_native.py, which established this exact pattern for the
# same shape of cell.
_EXECUTION_VARIANT = execution_variant_key(
    provider="claude",
    assertion_id=_ASSERTION_ID,
    scenario_id=_SCENARIO_ID,
    variant=None,
)

REGISTRATION = ProducerRegistration(
    producer_id="claude.turn_boundary_quiescent.v1",
    producer_revision=4,
    scenario_id=_SCENARIO_ID,
    scenario_revision=5,
    assertion_cells=((_ASSERTION_ID, None),),
    providers=("claude",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    # This producer only exercises the Helm launch path (`longhouse claude`).
    # The schema's capability contexts also name `console`; this producer does
    # not prove that half, so it deliberately does not claim it here.
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "managed_claude_session_launched",
        "assistant_turn_completed",
        "served_activity_settled_quiescent",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("claude_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "session_launch_receipt",
        "activity_state_samples",
        "session_close_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("claude_helm_process_exited",),
    implementation="server/zerg/qa/claude_turn_boundary_quiescent.py",
    oracle_source="server/zerg/qa/managed_claude_live.py",
    # managed_claude_live.py has no per-observation judge function (unlike
    # provider_resume_oracles.native_resume_assertions); it owns the real
    # launch/send/observe loop this producer's own quiescence check is layered
    # on top of. run_managed_claude_live_session is the entrypoint whose
    # primitives (channel readiness, marker-send, transcript observation) this
    # producer reuses; see claude_live_session_support.py for exactly which
    # pieces are imported. The quiescent-at-turn-boundary judgment itself is
    # local to this file because managed_claude_live.py has no served-state
    # concept at all -- see the report to the calling agent for this
    # producer's rationale.
    oracle_entrypoint="run_managed_claude_live_session",
    executable_module="zerg.qa.claude_turn_boundary_quiescent",
)

_ARTIFACT_KIND = "claude_turn_boundary_quiescent_result"


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    isolation_root = Path(tempfile.mkdtemp(prefix="lhx-claude-turnb-", dir="/tmp"))
    workspace = isolation_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    marker = f"LONGHOUSE_CLAUDE_TURN_BOUNDARY_{uuid.uuid4().hex}"
    prompt = f"Reply with exactly {marker} and nothing else."

    shipper = None
    session = None
    close_receipt: dict[str, Any] = {"not_started": True, "alive_after_close": False}
    try:
        shipper, environment = start_machine_and_shipper(args, isolation_root=isolation_root, evidence_root=root)
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        # Claude persists first-run onboarding (theme/API-key/trust) outside
        # the Longhouse channel state. Launching the managed facade against
        # this fresh disposable profile without completing it first leaves
        # the session stuck at the wizard forever -- see
        # claude_coordination_awareness_create.py's identical fix for the
        # full rationale.
        home, longhouse_home = isolation_paths(isolation_root)
        onboarding = prepare_claude_profile(
            binary=args.provider_bin,
            home=home,
            workspace=workspace,
            environment=environment,
            recording=root / "claude-onboarding.tty",
        )
        write_json(root / "claude-onboarding-receipt.json", onboarding)

        launch_env = claude_launch_environment(
            environment,
            claude_bin=args.provider_bin,
            engine=args.engine,
            model=args.model,
            longhouse_home=longhouse_home,
        )
        session, session_id, provider_session_id = launch_claude_session(
            workspace=workspace,
            project=args.project,
            name="Longhouse turn-boundary quiescence qualification",
            env=launch_env,
            terminal_path=root / "terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        write_json(root / "session-launch-receipt.json", {"session_id": session_id, "workspace": str(workspace)})

        # Coordination-channel messages are attributed, untrusted peer input.
        # This is a turn-boundary probe, so submit a real Helm user turn rather
        # than asking Claude to obey a channel-delivered command.
        session.submit_line(prompt)
        transcript_path, transcript_line, transcript_timestamp = await_assistant_marker(
            session_id=session_id,
            marker=marker,
            timeout=args.response_timeout_secs,
            home=Path(environment["HOME"]),
            provider_session_id=provider_session_id,
        )
        turn_completed_at = now_iso()

        # Poll the served fact *before* closing the session. Closing first
        # would let disposition/close-driven activity normalization mask a
        # real "never reached quiescent" bug behind a session that merely
        # stopped existing.
        returned_to_quiescent, time_to_quiescent_secs, activity_samples = wait_for_served_quiescent(
            api_url=args.api_url,
            token=args.agents_token,
            session_id=session_id,
            timeout=args.quiescent_timeout_secs,
        )
        write_json(
            root / "activity-state-samples.json",
            {"samples": activity_samples, "turn_completed_at": turn_completed_at},
        )

        close_receipt = close_session(session)
        session = None
        write_json(root / "session-close-receipt.json", close_receipt)
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            shipper = None
        write_claude_cleanup_aggregate(
            root,
            producer_id=REGISTRATION.producer_id,
            required_cleanup=REGISTRATION.required_cleanup,
            outcomes={"claude_helm_process_exited": close_receipt.get("alive_after_close") is False},
            diagnostics={"session": close_receipt},
        )

        run.observation = {
            "session_id": session_id,
            "marker": marker,
            "assistant_response_transcript_path": transcript_path,
            "assistant_response_transcript_line": transcript_line,
            "assistant_response_observed_at": transcript_timestamp,
            "turn_completed_at": turn_completed_at,
            "returned_to_quiescent": returned_to_quiescent,
            "time_to_quiescent_secs": round(time_to_quiescent_secs, 3),
            "activity_state_samples": activity_samples,
            "session_closed_cleanly": close_receipt.get("exit_code") == 0 and not close_receipt.get("alive_after_close"),
        }
        run.assertions = {"activity_returns_to_quiescent_at_turn_boundary": returned_to_quiescent is True}
    except Exception:  # noqa: BLE001 - close what the run owned, then let the shell type the failure
        if session is not None:
            try:
                close_receipt = close_session(session)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error below
                close_receipt = failed_session_close_receipt(session, cleanup_exc)
        if shipper is not None:
            try:
                write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error below
                close_receipt["shipper_stop_error"] = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            shipper = None
        cleanup_recording_error = None
        try:
            write_claude_cleanup_aggregate(
                root,
                producer_id=REGISTRATION.producer_id,
                required_cleanup=REGISTRATION.required_cleanup,
                outcomes={"claude_helm_process_exited": close_receipt.get("alive_after_close") is False},
                diagnostics={"session": close_receipt},
            )
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error below
            cleanup_recording_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        if cleanup_recording_error:
            run.fields["cleanup_recording_error"] = cleanup_recording_error
        raise
    finally:
        if shipper is not None:
            try:
                write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception:  # noqa: BLE001 - result already retains the causal failure
                pass
        shutil.rmtree(isolation_root, ignore_errors=True)


def run_turn_boundary_scenario(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args))


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--launch-timeout-secs", type=float, default=60.0)
    parser.add_argument("--response-timeout-secs", type=float, default=120.0)
    parser.add_argument("--quiescent-timeout-secs", type=float, default=90.0)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind=_ARTIFACT_KIND,
    failure_code="claude_turn_boundary_scenario_failed",
    needs=("evidence_root", "repo_root", "engine", "provider_bin"),
    binaries=(("engine", "longhouse_engine_missing"), ("provider_bin", "claude_binary_missing")),
    runtime_host=True,
    variants=(_EXECUTION_VARIANT,),
    provider_bin_aliases=("--claude-bin",),
    model_env="ANTHROPIC_MODEL",
    add_arguments=_add_arguments,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_turn_boundary_scenario, argv)


if __name__ == "__main__":
    raise SystemExit(main())
