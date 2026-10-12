#!/usr/bin/env python3
"""Bounded OMP Helm qualification using the native extension channel.

The lifecycle is the Pi family's (`zerg.qa.pi_family_helm`). This module is
what is OMP's: the registration, what differs for OMP (`FAMILY`), the order of
its steps, and its cells.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa import pi_family_helm as helm
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.failed_results import reached_only
from zerg.qa.omp_console_producer import omp_native_model_evidence
from zerg.qa.pi_family_helm import Family
from zerg.qa.pi_family_helm import Lifecycle
from zerg.qa.pi_family_turn_oracle import NEGATIVE_CONTROLS
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import ProducerFailure
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

SCENARIO_ID = "omp_helm_lifecycle"
ASSERTIONS = (
    "omp_helm_launch_registration",
    "omp_helm_send_idle",
    "omp_helm_follow_up_native",
    "omp_helm_steer_active",
    "omp_helm_abort_native",
    "omp_helm_terminate_owned",
    "omp_helm_cold_resume_exact_file",
    "omp_helm_stale_owner_refused",
    "omp_helm_native_replacement_bound",
)
_VARIANTS = tuple(execution_variant_key(provider="omp", assertion_id=item, scenario_id=SCENARIO_ID, variant=None) for item in ASSERTIONS)

REGISTRATION = ProducerRegistration(
    producer_id="omp.helm_lifecycle.v1",
    producer_revision=10,
    scenario_id=SCENARIO_ID,
    # 22: a run that held every assertion over a changing evidence tree is a typed failure.
    # 23: the lifecycle is the Pi family's one engine, and its evidence the family's one shape.
    scenario_revision=23,
    assertion_cells=tuple((assertion, None) for assertion in ASSERTIONS),
    providers=("omp",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "omp_native_extension_channel_bound",
        "omp_agent_end_settlement_observed",
        "omp_native_archive_bound",
        "omp_transcript_shipper_started",
        "omp_transcript_flush_completed",
        "omp_runtime_transcript_converged",
        "omp_owned_processes_dead",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("omp_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "helm_receipt",
        "native_settlement_receipt",
        "transcript_flush_receipt",
        "transcript_shipper_receipt",
        "runtime_convergence_receipt",
        "provider_source_retention",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "provider_process_dead",
        "process_group_dead",
        "no_orphan_provider_processes",
        "canary_session_hidden",
        "isolation_removed",
    ),
    implementation="server/zerg/qa/omp_helm_lifecycle.py",
    oracle_source="server/zerg/qa/omp_helm_lifecycle.py",
    oracle_entrypoint="omp_helm_lifecycle_assertions",
    executable_module="zerg.qa.omp_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)
PROFILE = "omp_helm_v1"
_PROFILE = identity.IdentityProfile(
    provider="omp",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(version_prefix=r"omp/"),
    oracle_source=Path(__file__),
)


# What proves each assertion's step was reached and its verdict decided. A step
# records itself only once it has run, so a record's presence is the proof. A
# run that errors reports a verdict only for assertions whose markers are all
# present and non-empty: an unreached step is absent from the map (the factory
# files "no verdict for assertion" as harness), never False (a product finding)
# and never True. Launch registration also needs the lifecycle to have run to
# its end, so an errored run never judges launch: harness, the conservative
# answer. Tests pin that every marker is written by the run.
REACHED_MARKERS: dict[str, tuple[str, ...]] = {
    "omp_helm_launch_registration": ("channel_binding", "control_identity", "settlement", "completed"),
    "omp_helm_send_idle": ("controls.send",),
    "omp_helm_follow_up_native": ("controls.follow_up",),
    "omp_helm_steer_active": ("controls.steer", "steer_turn_verdict"),
    "omp_helm_abort_native": ("controls.abort", "abort_turn_verdict"),
    "omp_helm_terminate_owned": ("terminate_verdict", "cleanup", "settlement"),
    "omp_helm_cold_resume_exact_file": ("controls.cold_resume",),
    "omp_helm_stale_owner_refused": ("controls.stale_owner",),
    "omp_helm_native_replacement_bound": ("controls.replacement",),
}
_HOST_CONTROLS = ("send", "follow_up", "steer", "abort", "replacement", "terminate", "final_terminate")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def reached_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    """The verdicts of the steps this observation actually reached."""

    return reached_only(omp_helm_lifecycle_assertions(observation), observation, REACHED_MARKERS)


def omp_helm_lifecycle_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    """OMP's cells, from the lifecycle's record."""

    controls = _mapping(observation.get("controls"))
    records = {name: _mapping(controls.get(name)) for name in (*_HOST_CONTROLS, "launch", "stale_owner", "cold_resume")}

    def ack(record: Mapping[str, Any], command: str = "command", state: str = "state") -> bool:
        """The Runtime Host took the command for the owner that was current, and answered the request that was made."""

        return helm._channel_command_evidence(_mapping(record.get(command)), _mapping(record.get(state)))["channel_ack_bound"] is True

    def native(record: Mapping[str, Any], evidence: str = "evidence") -> bool:
        """The reply is one row of the provider's own file, inside the turn's window, saying the marker once."""

        found = _mapping(record.get(evidence))
        return found.get("native_source_bound") is True and found.get("marker_count") == 1

    cleanup_ok = helm._helm_cleanup_ready(_mapping(observation.get("cleanup")))
    channel = _mapping(observation.get("channel_binding"))
    settlement = _mapping(observation.get("settlement"))
    settlement_ok = (
        settlement.get("status") == "pass"
        and settlement.get("agent_end_terminal") is True
        and settlement.get("agent_end_evidence_shape") is True
        and settlement.get("native_archive_bound") is True
        and settlement.get("native_session_header_count") == 1
        and settlement.get("malformed_source") is False
        and settlement.get("agent_settled_is_not_completion_contract") is True
    )
    convergence = _mapping(observation.get("convergence"))
    send, follow_up, steer, abort = records["send"], records["follow_up"], records["steer"], records["abort"]
    replacement, resume, stale = records["replacement"], records["cold_resume"], records["stale_owner"]
    steer_state = _mapping(steer.get("active_state"))
    resume_state, resumed_from = _mapping(resume.get("state")), _mapping(resume.get("before"))
    resume_terminal = _mapping(resume.get("terminal_evidence"))
    return {
        "omp_helm_launch_registration": (
            observation.get("observation_scope") == "scenario"
            and all(
                channel.get(fact) is True
                for fact in (
                    "ready",
                    "session_id_present",
                    "native_session_id_present",
                    "connection_id_present",
                    "lease_generation_present",
                    "session_file_present",
                )
            )
            and _mapping(observation.get("shipper")).get("started") is True
            # The first turn and the last are served: the flush shipped and the Runtime Host converged on each.
            and all(_mapping(convergence.get(turn)).get("status") == "pass" for turn in ("launch", "resume"))
            and helm._runtime_control_identity_is_complete(observation.get("control_identity"))
            and all(_mapping(records[name].get("command")).get("transport") == "runtime_host_agents_api" for name in _HOST_CONTROLS)
            and settlement_ok
        ),
        "omp_helm_send_idle": ack(send) and native(send),
        "omp_helm_follow_up_native": (
            _mapping(follow_up.get("active_state")).get("phase") in {"running", "thinking"}
            and ack(follow_up, "active_command", "active_state")
            and follow_up.get("queued_not_sent") is True
            and ack(follow_up)
            and native(follow_up)
        ),
        "omp_helm_steer_active": (
            ack(steer, "active_command", "active_state")
            and ack(steer, "command", "active_state")
            and native(steer)
            and _mapping(observation.get("steer_turn_verdict")).get("passed") is True
            and steer_state.get("phase") in {"running", "thinking"}
            and _mapping(steer.get("evidence")).get("native_session_id") == steer_state.get("native_session_id")
        ),
        "omp_helm_abort_native": (
            _mapping(abort.get("command")).get("accepted") is True
            and channel.get("ready") is True
            and _mapping(abort.get("evidence")).get("channel_source_bound") is True
            and _mapping(abort.get("evidence")).get("terminal") is True
            and _mapping(observation.get("abort_turn_verdict")).get("passed") is True
            and _mapping(observation.get("abort_turn_verdict")).get("after_send_accepted") is True
        ),
        "omp_helm_terminate_owned": _mapping(observation.get("terminate_verdict")).get("passed") is True and cleanup_ok and settlement_ok,
        "omp_helm_cold_resume_exact_file": (
            # The same native session and the same file under a new owner.
            bool(resume_state.get("native_session_id"))
            and resume_state.get("native_session_id") == resumed_from.get("native_session_id")
            and resume_state.get("session_file") == resumed_from.get("session_file")
            and native(resume)
            # The phrase the old owner was given comes back from the restored history, which the resume prompt does not hold.
            and native(replacement, "context_evidence")
            and native(resume, "context_evidence")
            and native(resume, "prompt_evidence")
            and bool(resume.get("context_phrase"))
            and str(resume.get("context_phrase")) not in str(resume.get("prompt"))
            and _mapping(resume.get("agent_end")).get("type") == "agent_end"
            and resume_terminal.get("channel_source_bound") is True
            and resume_terminal.get("terminal") is True
        ),
        "omp_helm_stale_owner_refused": (
            _mapping(stale.get("second_launcher")).get("owner_refusal_observed") is True
            and _mapping(stale.get("response")).get("ok") is False
            and stale.get("error_code") == "stale_channel"
        ),
        "omp_helm_native_replacement_bound": (
            ack(replacement)
            and bool(_mapping(replacement.get("state")).get("native_session_id"))
            and _mapping(replacement.get("state")).get("native_session_id") != _mapping(replacement.get("before")).get("native_session_id")
            and native(replacement)
        ),
    }


def _exact_marker_prompt(marker: str) -> str:
    """Make sequential marker probes distinguish a fresh request."""

    return f"New machine-check request. The active marker for this request is {marker}. Reply with exactly {marker} and no other text."


def _setup_marker_prompt(marker: str, *, setup: str) -> str:
    """Preserve setup instructions while requiring one exact marker reply."""

    return (
        f"{setup.rstrip()}\n\n"
        "New machine-check request. After completing that setup, the active "
        f"marker for this request is {marker}. Reply with exactly {marker} "
        "and no other text."
    )


def _launch_argv(
    args: argparse.Namespace,
    *,
    workspace: Path,
    prompt: str,
    resume_session: str | None = None,
) -> list[str]:
    argv = [
        str(args.longhouse_cli),
        "omp",
        "--cwd",
        str(workspace),
        "--omp-bin",
        str(args.provider_bin),
        "--url",
        str(args.api_url),
        "--prompt",
        prompt,
    ]
    if getattr(args, "model", None):
        argv.extend(("--model", str(args.model)))
    if resume_session is not None:
        argv.extend(("--resume-session", resume_session))
    return argv


FAMILY = Family(
    provider="omp",
    name="OMP",
    marker_prefix="OMP_HELM",
    live_env="LONGHOUSE_OMP_LIVE",
    id_key="native_session_id",
    launch_argv=_launch_argv,
    environment=lambda home, _args: {
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "LONGHOUSE_OMP_DATA_DIR": str(home / ".local" / "share" / "omp"),
        "LONGHOUSE_OMP_SESSION_DIR": str(home / ".local" / "share" / "omp" / "sessions"),
    },
    prepare=lambda life: (
        life.env.update(LONGHOUSE_QA_OMP_BACKGROUND_CAPTURE=str(life.root / "omp-background-native-frames.ndjson"))
        if life.background
        else None
    ),
    prompts={
        "launch": _exact_marker_prompt("{marker}"),
        "send": _exact_marker_prompt("{marker}"),
        "busy": _setup_marker_prompt(
            "{marker}", setup="Use the bash tool to run `sleep 8` in the foreground (not as a named or background job), then"
        ),
        "follow_up": _exact_marker_prompt("{marker}"),
        "steer": "Stop: skip every remaining step and reply with exactly {marker} and no other text.",
        "after_abort": _exact_marker_prompt("{marker}"),
    },
    cells=omp_helm_lifecycle_assertions,
    reached=REACHED_MARKERS,
    converged=lambda served: (
        served["exact_served_identity"] and served["marker_bound_durable_event"] and served["served_path"] == "canonical_session_detail"
    ),
    control_bound=helm._served_control_identity,
    is_idle=lambda state: state.get("phase") == "idle",
    timeouts={"launch": 30, "busy": 30, "identity": 90, "replace": 60, "resume": 30, "resume_reply": 180, "resume_context": 300},
    homes=("home", "home/.longhouse"),
    channel_terminal=True,
    host_controls=True,
    identity_at_launch=True,
    paginated_events=True,
    binds_resume_prompt=True,
    retirement_claims=True,
    settles_between_turns=True,
    flush_must_complete=True,
    evidence_must_be_final=True,
)


def _steps(life: Lifecycle) -> None:
    life.launch()
    if life.background:
        return
    life.second_launcher()
    life.send_idle()
    life.follow_up()
    life.steer()
    life.abort()
    # No retirement wait between the abort and `/new`. Abort ends the turn, not
    # the run: the managed run is keyed to the launch and `/new` keeps it, so the
    # served run correctly stays `running` until terminate.
    life.replace(stale_frame=True)
    life.terminate()
    life.cold_resume()


def _reach(run: Run, args: argparse.Namespace) -> None:
    helm.reach(FAMILY, _steps, run, args)


def run_omp_helm(args: argparse.Namespace, *, raises: bool = False) -> dict[str, object]:
    def body(run: Run) -> None:
        _reach(run, args)
        final = run.fields["final_evidence"]
        if not run.controlled and all(run.assertions.values()) and not (final["cleanup_ready"] and final["manifest_stable"]):
            # Every assertion held over an evidence tree that was still
            # changing: not a pass, and not a verdict against the subject.
            raise ProducerFailure("omp_helm_evidence_not_final", "the evidence tree changed after every assertion held")

    return PRODUCER.execute(args.evidence_root, body, raises=raises)


def _request_args(
    request_path: Path,
    output_root: Path,
    *,
    request: Mapping[str, Any] | None = None,
    variant: str | None = None,
    agents_token: str | None = None,
) -> argparse.Namespace:
    request = dict(request) if request is not None else json.loads(request_path.read_text(encoding="utf-8"))
    runtime_token = agents_token if agents_token is not None else os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN")
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        engine=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or ""),
        longhouse_cli=Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse"),
        api_url=os.environ.get("LONGHOUSE_RUNTIME_API_URL"),
        agents_token=runtime_token,
        model=os.environ.get("LONGHOUSE_OMP_QUALIFICATION_MODEL", ""),
        variant=variant,
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    request = identity.load_request(
        request_path,
        provider="omp",
        profile=PROFILE,
        version_grammar=_PROFILE.version_grammar,
    )
    runtime_token = str(os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN") or "")

    def execute(binary: Path, evidence_root: Path):
        # The semantic entrypoint executes every registered cell; selection is
        # only meaningful for the direct CLI path. The validated request owns
        # the provider binary/version, while the runtime token remains secret.
        run_args = _request_args(request_path, evidence_root, request=request, agents_token=runtime_token)
        run_args.provider_bin = binary
        result = run_omp_helm(run_args, raises=True)
        observation = dict(result.get("observation") or {})
        model_evidence = omp_native_model_evidence(
            evidence_root,
            source_canary=PROFILE,
            api_key_configured=bool(str(os.environ.get("OPENROUTER_API_KEY") or "").strip()),
            qualification_model=run_args.model,
        )
        if model_evidence is not None:
            observation["live_model_evidence"] = model_evidence
        assertions = omp_helm_lifecycle_assertions(observation)
        semantic_assertions = tuple(
            semantic.SemanticAssertion(
                assertion,
                AssertionOutcome.PASS if passed else AssertionOutcome.SEMANTIC_FAIL,
                EvidenceClass.LIVE_TOKEN,
            )
            for assertion, passed in assertions.items()
        )
        return (
            observation,
            semantic_assertions,
            tuple(
                dict.fromkeys(
                    value
                    for value in (
                        runtime_token,
                        str(os.environ.get("OPENROUTER_API_KEY") or "").strip(),
                    )
                    if value
                )
            ),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=ASSERTIONS,
        executor=execute,
        oracle_source=Path(__file__),
        scenario_revision=REGISTRATION.scenario_revision,
    )


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="omp_helm_lifecycle_result",
    failure_code="omp_helm_lifecycle_failed",
    variants=_VARIANTS,
    negative_controls=tuple(NEGATIVE_CONTROLS),
    # A run that raised reports verdicts only for the steps it reached.
    reached=REACHED_MARKERS,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_omp_helm, argv)


if __name__ == "__main__":
    raise SystemExit(main())
