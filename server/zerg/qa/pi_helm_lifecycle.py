#!/usr/bin/env python3
"""Bounded Pi Helm qualification using the native extension channel.

The lifecycle is the Pi family's (`zerg.qa.pi_family_helm`). This module is
what is Pi's: the registration, what differs for Pi (`FAMILY`), the order of
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
from zerg.qa.openrouter_routing import prepare_pi_openrouter_routing
from zerg.qa.pi_family_helm import Family
from zerg.qa.pi_family_helm import Lifecycle
from zerg.qa.pi_family_turn_oracle import NEGATIVE_CONTROLS
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.producer_shell import write_json as _write_json
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

SCENARIO_ID = "pi_helm_lifecycle"
PROFILE = "pi_helm_v1"
_PROFILE = identity.IdentityProfile(
    provider="pi",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(),
    oracle_source=Path(__file__),
)
ASSERTIONS = (
    "pi_helm_launch_registration",
    "pi_helm_native_tool_taxonomy",
    "pi_helm_send_idle",
    "pi_helm_steer_active",
    "pi_helm_follow_up_native",
    "pi_helm_abort_native",
    "pi_helm_terminate_owned",
    "pi_helm_reload_rebind",
    "pi_helm_session_replacement_rebind",
    "pi_helm_cold_resume_exact_file",
    "pi_helm_stale_owner_refused",
)
_VARIANTS = tuple(execution_variant_key(provider="pi", assertion_id=item, scenario_id=SCENARIO_ID, variant=None) for item in ASSERTIONS)

REGISTRATION = ProducerRegistration(
    producer_id="pi.helm_lifecycle.v1",
    producer_revision=3,
    scenario_id=SCENARIO_ID,
    # 17: a run that raised outside its own handling leaves a complete result; --model is required.
    # 18: the lifecycle is the Pi family's one engine, and its evidence the family's one shape; an exited
    # process counts as stopped before its parent collects it (live_kit.pid_dead).
    # 19: send, steer, abort and terminate go through the Runtime Host, as a remote user's do; the engine's local
    # command is proven beside them, in turns of its own and with the last terminate.
    # 20: a send that is not accepted starts no turn, and the run ends there and names it, not at a later wait.
    # 21: a second launcher started against the live owner is refused, and the stale-owner cell rests on that too.
    # 22: control of the session is held to the owner's lease at launch, at cold resume and at the end, not only
    # after a reload or a replacement.
    # 23: the served events are read to their end, page by page under one generation, not as one page of 200.
    scenario_revision=23,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=("pi",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "stock_longhouse_pi_helm_launch",
        "pi_native_session_jsonl_bound",
        "pi_helm_channel_command_accepted",
        "pi_helm_reload_and_session_replacement_observed",
        "pi_helm_exact_cold_resume",
        "pi_helm_stale_owner_rejected",
        "owned_processes_dead",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("pi_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "helm_receipt",
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
        "served_run_retired",
    ),
    implementation="server/zerg/qa/pi_helm_lifecycle.py",
    oracle_source="server/zerg/qa/pi_helm_lifecycle.py",
    oracle_entrypoint="pi_helm_lifecycle_assertions",
    executable_module="zerg.qa.pi_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)

_STALE_REFUSALS = frozenset({"stale_channel", "stale_generation", "session_not_attached"})
_HOST, _LOCAL = "runtime_host_agents_api", "local_engine_command"
_BOUND = ("one_session", "one_thread", "provider_session_bound")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def pi_helm_lifecycle_assertions(observation: Mapping[str, Any]) -> dict[str, bool]:
    """Pi's cells, from the lifecycle's record."""

    controls = _mapping(observation.get("controls"))
    names = (
        "launch",
        "send",
        "follow_up",
        "steer",
        "abort",
        "local_send",
        "local_steer",
        "local_abort",
        "reload",
        "replacement",
        "terminate",
        "cold_resume",
        "final_terminate",
        "stale_owner",
    )
    records = {name: _mapping(controls.get(name)) for name in names}

    def accepted(record: Mapping[str, Any], command: str = "command") -> bool:
        return _mapping(record.get(command)).get("accepted") is True

    def through(transport: str, record: Mapping[str, Any], *commands: str) -> bool:
        """Each of the record's commands was accepted, and went the way the cell says it went."""

        return all(accepted(record, command) and _mapping(record.get(command)).get("transport") == transport for command in commands)

    def steered(record: Mapping[str, Any], verdict: str) -> bool:
        return busy(record) and answered(record) and _mapping(observation.get(verdict)).get("passed") is True

    def aborted(record: Mapping[str, Any], verdict: str) -> bool:
        native = _mapping(record.get("evidence"))
        return (
            busy(record)
            and bool(record.get("settled_state"))
            and _mapping(record.get("settled_state")).get("phase") not in {"running", "thinking"}
            and native.get("observed") is True
            and bool(native.get("retained_source_path"))
            and isinstance(native.get("aborted_row_id"), str)
            and isinstance(native.get("aborted_row_source_offset"), int)
            and _mapping(observation.get(verdict)).get("passed") is True
            and _mapping(observation.get(verdict)).get("after_send_accepted") is True
        )

    def answered(record: Mapping[str, Any], evidence: str = "evidence") -> bool:
        """The turn's window of the provider's own file holds rows, and one of them answers with the marker."""

        found = _mapping(record.get(evidence))
        return (found.get("window_row_count") or 0) > 0 and (found.get("assistant_answer_rows") or 0) >= 1

    def served(record: Mapping[str, Any], turn: str = "served") -> Mapping[str, Any]:
        """What the Runtime Host served for the record's turn (the record names the convergence it is kept under)."""

        return _mapping(_mapping(observation.get("convergence")).get(record.get(turn)))

    def bound(record: Mapping[str, Any], turn: str = "served", facts: tuple[str, ...] = _BOUND) -> bool:
        """What is served for the turn is one session and one thread (and, where asked, the provider's own session)."""

        binding = _mapping(served(record, turn).get("binding"))
        return all(binding.get(fact) is True for fact in facts)

    def flushed(label: str) -> bool:
        return _mapping(_mapping(observation.get("flush")).get(label)).get("status") == "pass"

    def control_bound(label: str) -> bool:
        """The Runtime Host served control of this session under the lease of the owner recorded at ``label``."""

        receipt = _mapping(_mapping(observation.get("control_identity")).get(label))
        actions = _mapping(receipt.get("actions"))
        return (
            bool(receipt.get("expected_subject_key"))
            and receipt.get("control_subject_key") == receipt.get("expected_subject_key")
            and receipt.get("session_id") == observation.get("session_id")
            and receipt.get("served_path") == "canonical_session_detail"
            and helm._owner_identity_is_complete(receipt.get("owner_identity"))
            # What Pi's lifecycle waits for before it uses a control: a send that is offered and a terminate that is named.
            and actions.get("send_input") == "available"
            and "terminate" in actions
        )

    def busy(record: Mapping[str, Any]) -> bool:
        return _mapping(record.get("active_state")).get("phase") in {"running", "thinking"}

    identities = _mapping(observation.get("control_identity"))
    cleanup = _mapping(observation.get("cleanup"))
    cleanup_ok = helm._helm_cleanup_ready(cleanup) and cleanup.get("cleanup_errors", []) == []
    launch, send, follow_up, steer, abort = (records[name] for name in ("launch", "send", "follow_up", "steer", "abort"))
    local_send, local_steer, local_abort = (records[name] for name in ("local_send", "local_steer", "local_abort"))
    reload, replacement, resume, stale = (records[name] for name in ("reload", "replacement", "cold_resume", "stale_owner"))
    launch_state = _mapping(launch.get("state"))
    taxonomy = _mapping(observation.get("native_taxonomy"))
    follow_request = _mapping(_mapping(follow_up.get("command")).get("request"))
    follow_native = _mapping(follow_up.get("evidence"))
    reload_before, reload_after = _mapping(reload.get("before")), _mapping(reload.get("state"))
    replaced_before, replaced = _mapping(replacement.get("before")), _mapping(replacement.get("state"))
    replaced_file = _mapping(_mapping(replacement.get("evidence")).get("native_file"))
    resume_state, resumed_from = _mapping(resume.get("state")), _mapping(resume.get("before"))
    resume_native, recall = _mapping(resume.get("evidence")), _mapping(resume.get("context_evidence"))
    seed = _mapping(replacement.get("context_evidence"))
    phrase = resume.get("context_phrase")
    recall_served = served(resume, "context_served")
    return {
        "pi_helm_launch_registration": (
            bool(_mapping(observation.get("shipper")).get("machine_name"))
            and launch_state.get("provider") == "pi"
            and launch_state.get("ready") is True
            and launch_state.get("status") in {"ready", "running"}
            and bool(str(launch_state.get("session_dir") or ""))
            and bool(str(launch_state.get("native_session_id") or ""))
            and flushed("launch")
            and bound(launch)
            and served(launch).get("served_controls") is True
            and control_bound("initial")
        ),
        "pi_helm_native_tool_taxonomy": bool(taxonomy.get("tool_pairs")) and not bool(taxonomy.get("tool_calls_without_results")),
        # Each control is proven the way a remote user's goes, through the Runtime Host, and once more through the
        # engine's local command (the `local_*` records and the last terminate).
        "pi_helm_send_idle": (
            through(_HOST, send, "command")
            and answered(send)
            and flushed("send")
            and bound(send, facts=_BOUND[:2])
            and through(_LOCAL, local_send, "command")
            and answered(local_send)
        ),
        "pi_helm_steer_active": (
            through(_HOST, steer, "active_command", "command")
            and steered(steer, "steer_turn_verdict")
            and through(_LOCAL, local_steer, "active_command", "command")
            and steered(local_steer, "local_steer_turn_verdict")
        ),
        "pi_helm_follow_up_native": (
            through(_HOST, follow_up, "active_command", "command")
            and busy(follow_up)
            and follow_request.get("method") == "POST"
            and str(follow_request.get("path") or "").startswith("/api/agents/sessions/")
            and str(follow_request.get("path") or "").endswith("/input")
            and follow_up.get("queued_not_sent") is True
            and isinstance(follow_native.get("marker"), str)
            and _mapping(follow_request.get("payload")).get("text") == FAMILY.prompts["follow_up"].format(marker=follow_native["marker"])
            and answered(follow_up)
            # The queued send was delivered once, as one row.
            and follow_native.get("user_marker_rows") == 1
            and follow_native.get("user_marker_occurrences") == 1
        ),
        "pi_helm_abort_native": (
            through(_HOST, abort, "active_command", "command")
            and aborted(abort, "abort_turn_verdict")
            and through(_LOCAL, local_abort, "active_command", "command")
            and aborted(local_abort, "local_abort_turn_verdict")
            and cleanup_ok
        ),
        "pi_helm_terminate_owned": (
            through(_HOST, records["terminate"], "command")
            and through(_LOCAL, records["final_terminate"], "command")
            and _mapping(records["final_terminate"].get("stopped_state")).get("status") == "pass"
            and records["final_terminate"].get("terminals_exited") is True
            and cleanup.get("provider_process_dead") is True
            and cleanup.get("process_group_dead") is True
            and cleanup_ok
        ),
        "pi_helm_reload_rebind": (
            bool(reload_after.get("connection_id"))
            and reload_after.get("connection_id") != reload_before.get("connection_id")
            and bool(reload_after.get("session_id"))
            and reload_after.get("session_id") == reload_before.get("session_id")
            and bool(reload_after.get("lease_generation"))
            and reload_after.get("lease_generation") != reload_before.get("lease_generation")
            and accepted(reload, "send")
            and (_mapping(reload.get("evidence")).get("assistant_answer_rows") or 0) >= 1
            and bound(reload, facts=_BOUND[:2])
            and _mapping(served(reload).get("binding")).get("provider") == "pi"
            and served(reload).get("served_controls") is True
        ),
        "pi_helm_session_replacement_rebind": (
            bool(replaced.get("session_id"))
            and replaced.get("session_id") == replaced_before.get("session_id")
            and bool(replaced.get("native_session_id"))
            and replaced.get("native_session_id") != replaced_before.get("native_session_id")
            # A new file, present, whose header names the new session.
            and bool(_mapping(replacement.get("native_file_before")).get("path"))
            and replaced_file.get("present") is True
            and replaced_file.get("path") != _mapping(replacement.get("native_file_before")).get("path")
            and _mapping(replacement.get("evidence")).get("native_header_session_id") == replaced.get("native_session_id")
            and bound(replacement, facts=_BOUND[:2])
        ),
        "pi_helm_cold_resume_exact_file": (
            # A new run of the same native session, in the same file.
            bool(resume_state.get("run_id"))
            and resume_state.get("run_id") != _mapping(_mapping(observation.get("owners")).get("initial")).get("run_id")
            and resume_state.get("native_session_id") == resumed_from.get("native_session_id")
            and bool(_mapping(resume_native.get("native_file")).get("path"))
            and _mapping(resume_native.get("native_file")).get("path") == _mapping(resume.get("native_file_before")).get("path")
            and resume_native.get("native_header_session_id") == resume_state.get("native_session_id")
            and answered(resume)
            and flushed("resume")
            and bound(resume)
            # The phrase the old owner was given once comes back from the restored history: the resume prompt
            # does not hold it, no user row of the resumed turn does, and the reply that recalls it is served.
            and isinstance(phrase, str)
            and bool(phrase)
            and phrase in str(replacement.get("prompt") or "")
            and seed.get("user_marker_rows") == 1
            and seed.get("user_marker_occurrences") == 1
            and bool(resume.get("prompt"))
            and phrase not in str(resume.get("prompt"))
            and recall.get("marker") == phrase
            and (recall.get("assistant_answer_rows") or 0) >= 1
            and recall.get("user_marker_rows") == 0
            and recall.get("user_marker_occurrences") == 0
            and recall_served.get("marker") == phrase
            and bool(_mapping(recall_served.get("served_projection")).get("marker_event_ids"))
            and bound(resume, "context_served")
            # Control is the resumed owner's, when it starts and when its turn is over, and that is one owner.
            and control_bound("cold_resume")
            and control_bound("final")
            and _mapping(identities.get("final")).get("owner_identity") == _mapping(identities.get("cold_resume")).get("owner_identity")
        ),
        "pi_helm_stale_owner_refused": (
            # One session has one execution owner: a second launcher is turned away while the first is live,
            # and a frame from an owner that is no longer current is refused and reaches no provider.
            _mapping(stale.get("second_launcher")).get("owner_refusal_observed") is True
            and stale.get("accepted") is False
            and stale.get("error_code") in _STALE_REFUSALS
            and _mapping(stale.get("native_dispatch")).get("observed") is False
        ),
    }


def _launch_argv(args: argparse.Namespace, *, workspace: Path, prompt: str, resume_session: str | None = None) -> list[str]:
    argv = [str(args.longhouse_cli), "pi", "--cwd", str(workspace), "--pi-bin", str(args.provider_bin)]
    argv += ["--provider", "openrouter", "--model", args.model]
    if resume_session is not None:
        argv += ["--resume-session", resume_session]
    return [*argv, "--prompt", prompt]


def _prepare(life: Lifecycle) -> None:
    (life.provider_home / ".pi" / "agent" / "sessions").mkdir(mode=0o700, parents=True)
    # The first turn reads this file with a tool, which is what the tool-pair cell is judged on.
    life.proof_file = life.workspace / "pi-tool-proof.txt"
    life.proof_file.write_text(life.marker("TOOL") + "\n", encoding="utf-8")
    # One slow OpenRouter host must not stall a cell: pin the model's requests.
    _write_json(life.root / "openrouter-routing-receipt.json", prepare_pi_openrouter_routing(life.provider_home / ".pi", life.args.model))


def _converged(served: Mapping[str, Any]) -> bool:
    binding = _mapping(served.get("binding"))
    return (
        served["answers_converged"]
        and all(binding.get(fact) is True for fact in _BOUND)
        and binding.get("provider") == "pi"
        and served["served_controls"] is True
    )


FAMILY = Family(
    provider="pi",
    name="Pi",
    marker_prefix="PI_HELM",
    live_env="LONGHOUSE_PI_LIVE",
    id_key="provider_session_id",
    launch_argv=_launch_argv,
    environment=lambda home, args: {"PI_CODING_AGENT_DIR": str(home / ".pi"), "LONGHOUSE_ENGINE_BIN": str(args.engine)},
    prepare=_prepare,
    prompts={
        "launch": "Use the read tool to read {proof_file}, then reply with exactly {marker}.",
        "send": "Reply with exactly {marker}.",
        "busy": "Use the read tool repeatedly on {proof_file}, then reply with {marker}.",
        "follow_up": "After this turn, reply with {marker}.",
        "steer": "Stop: skip every remaining step and reply only with {marker}.",
        "after_abort": "Reply only with {marker}.",
    },
    cells=pi_helm_lifecycle_assertions,
    reached=None,
    converged=_converged,
    control_bound=helm._served_send_identity,
    is_idle=lambda state: state.get("phase") not in {"running", "thinking"},
    timeouts={"launch": 45, "busy": 10, "identity": 30, "replace": 30, "resume": 45, "resume_reply": 90, "resume_context": 90},
    homes=("provider-home", "longhouse"),
    tool_launch=True,
    host_controls=True,
    local_controls=True,
    identity_at_launch=True,
    terminate_proves_owners_dead=True,
    rebind_command="send_live",
    native_abort_row=True,
    local_health=True,
    records_misses=True,
    refused_command_ends_run=True,
    strict_resume_state=True,
    control_outlives_failure=True,
)


def _steps(life: Lifecycle) -> None:
    local = FAMILY.local_controls
    life.launch()
    life.second_launcher()
    life.send_idle()
    life.follow_up()
    life.steer()
    if local:
        life.send_idle(local=True)
        life.steer(local=True)
    life.reload()
    life.replace(stale_frame=False)
    life.abort()
    if local:
        life.abort(local=True)
    life.terminate()
    life.cold_resume()


def _reach(run: Run, args: argparse.Namespace) -> None:
    helm.reach(FAMILY, _steps, run, args)


def run_pi_helm_lifecycle(args: argparse.Namespace, *, raises: bool = False) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args), raises=raises)


def _request_args(
    request_path: Path,
    output_root: Path,
    *,
    request: dict[str, Any] | None = None,
) -> argparse.Namespace:
    request = dict(request) if request is not None else json.loads(request_path.read_text(encoding="utf-8"))
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        engine=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or ""),
        longhouse_cli=Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse"),
        api_url=os.environ.get("LONGHOUSE_RUNTIME_API_URL"),
        agents_token=os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"),
        model=os.environ.get("LONGHOUSE_PI_QUALIFICATION_MODEL", ""),
        variant=None,
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    request = identity.load_request(
        request_path,
        provider="pi",
        profile=PROFILE,
        version_grammar=_PROFILE.version_grammar,
    )
    runtime_token = str(os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN") or "")

    def execute(binary: Path, evidence_root: Path):
        run_args = _request_args(request_path, evidence_root, request=request)
        run_args.provider_bin = binary
        result = run_pi_helm_lifecycle(run_args, raises=True)
        observation = dict(result.get("observation") or result.get("diagnostic_observation") or {})
        assertions = dict(result.get("assertions") or result.get("diagnostic_assertions") or {})
        semantic_assertions = tuple(
            semantic.SemanticAssertion(
                assertion,
                AssertionOutcome.PASS if assertions.get(assertion) is True else AssertionOutcome.SEMANTIC_FAIL,
                EvidenceClass.LIVE_TOKEN,
            )
            for assertion in ASSERTIONS
        )
        return (
            observation,
            semantic_assertions,
            tuple(dict.fromkeys(value for value in (runtime_token, str(os.environ.get("OPENROUTER_API_KEY") or "").strip()) if value)),
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
    artifact_kind="pi_helm_lifecycle_result",
    failure_code="pi_helm_lifecycle_failed",
    # The factory names the qualification model: there is no default, and an
    # Anthropic model must never be routed through OpenRouter.
    needs=("evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin", "provider_version", "api_url", "agents_token", "model"),
    variants=_VARIANTS,
    model_env="LONGHOUSE_PI_QUALIFICATION_MODEL",
    negative_controls=tuple(NEGATIVE_CONTROLS),
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_pi_helm_lifecycle, argv)


if __name__ == "__main__":
    raise SystemExit(main())
