#!/usr/bin/env python3
"""Live OMP Console qualification through the native ``omp_print`` adapter."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

SCENARIO_ID = "omp_console_lifecycle"
ASSERTION_ID = "omp_console_turn_settled_and_bound"
SUPPORTED_VARIANT = lifecycle.SUPPORTED_VARIANT

REGISTRATION = ProducerRegistration(
    producer_id="omp.console_lifecycle.v1",
    producer_revision=4,
    scenario_id=SCENARIO_ID,
    scenario_revision=4,
    assertion_cells=((ASSERTION_ID, None),),
    providers=("omp",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("live_token",),
    observed_activity=(
        "runtime_host_turn_dispatch",
        "exact_session_thread_run_binding",
        "transcript_converged_exactly_once",
        "omp_agent_end_terminal_observed",
        "omp_stream_drained",
        "omp_native_archive_bound",
        "omp_continuation_context_recalled",
        "interrupt_contract_preserved",
        "post_interrupt_sendable",
        "no_orphan_provider_processes",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("omp_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "adapter_dispatch_receipt",
        "transcript_flush_receipt",
        "console_boundary_receipt",
        "provider_response_binding_receipt",
        "console_continuation_receipt",
        "interrupt_contract_receipt",
        "omp_settlement_receipt",
        "provider_source_retention",
        "cleanup_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "no_orphan_provider_processes", "canary_session_hidden"),
    implementation="server/zerg/qa/omp_console_producer.py",
    oracle_source="server/zerg/qa/omp_console_producer.py",
    oracle_entrypoint="omp_console_assertions",
    executable_module="zerg.qa.omp_console_producer",
    provider_artifact_required=True,
)
_VARIANT = execution_variant_key(provider="omp", assertion_id=ASSERTION_ID, scenario_id=SCENARIO_ID, variant=None)
PROFILE = "omp_print_v1"
_PROFILE = identity.IdentityProfile(
    provider="omp",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(),
    oracle_source=Path(__file__),
)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _native_message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text"))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    return ""


def _exact_session_retirement(receipt: Mapping[str, Any] | None, session_id: str | None) -> bool:
    return (
        isinstance(receipt, Mapping)
        and bool(session_id)
        and receipt.get("status") == "pass"
        and receipt.get("session_id") == session_id
        and receipt.get("hidden") is True
        and receipt.get("archived") is True
        and receipt.get("present_in_served_inventory") is False
    )


def _is_terminal_agent_end(event: Mapping[str, Any]) -> bool:
    is_terminal = event.get("isTerminal")
    if isinstance(is_terminal, bool):
        return is_terminal
    return event.get("willContinue") is False


def _native_settlement(root: Path) -> dict[str, object]:
    """Bind OMP's print terminal and native archive/tool evidence separately."""

    retention = _read_json(root / "provider-source-retention.json") or {}
    sources = retention.get("sources") if isinstance(retention.get("sources"), list) else []
    dispatch = _read_json(root / "adapter-dispatch-receipt.json") or {}
    binding = _read_json(root / "provider-response-binding-receipt.json") or {}
    expected_native_id = str(binding.get("provider_thread_id") or dispatch.get("provider_thread_id") or "")
    marker = str(binding.get("marker") or "")
    tool_marker = str(binding.get("tool_marker") or "")
    response_source_kind = binding.get("provider_response_source_kind")
    response_source_path = binding.get("provider_response_source_path")
    agent_settled_seen = False
    provider_response_terminal = False
    provider_response_terminal_shape = False
    provider_response_source_bound = False
    native_archive_bound = False
    native_session_id_bound = False
    native_marker_count = 0
    tool_call_ids: set[str] = set()
    tool_result_ids: set[str] = set()
    tool_output_marker_count = 0
    source_paths: list[str] = []
    malformed_source = False

    for item in sources:
        if not isinstance(item, Mapping) or item.get("retained") is not True:
            continue
        raw_path = item.get("path")
        raw_source = item.get("source")
        kind = item.get("kind")
        if not isinstance(raw_path, str) or not isinstance(raw_source, str):
            continue
        source_paths.append(raw_source)
        try:
            lines = (root / raw_path).read_bytes().splitlines()
        except OSError:
            continue
        events: list[Mapping[str, Any]] = []
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                malformed_source = True
                continue
            if isinstance(event, Mapping):
                events.append(event)
            else:
                malformed_source = True
        headers = [event for event in events if event.get("type") == "session" and event.get("id") == expected_native_id]
        assistant_events = [
            event
            for event in events
            if event.get("type") in {"message", "message_end"}
            and isinstance(event.get("message"), Mapping)
            and event["message"].get("role") == "assistant"
        ]
        marker_count = sum(_native_message_text(event["message"]).count(marker) for event in assistant_events if marker)
        native_marker_count = max(native_marker_count, marker_count)
        if any(event.get("type") == "agent_settled" for event in events):
            agent_settled_seen = True

        if kind == "source_path":
            native_session_id_bound = native_session_id_bound or len(headers) == 1
            native_archive_bound = native_archive_bound or (len(headers) == 1 and marker_count == 1)
            if len(headers) == 1:
                for event in events:
                    message = event.get("message")
                    if not isinstance(message, Mapping):
                        continue
                    role = message.get("role")
                    content = message.get("content")
                    blocks = content if isinstance(content, list) else []
                    if role == "assistant":
                        for block in blocks:
                            if isinstance(block, Mapping) and block.get("type") == "toolCall" and block.get("id"):
                                tool_call_ids.add(str(block["id"]))
                    elif role == "toolResult":
                        tool_call_id = message.get("toolCallId")
                        if isinstance(tool_call_id, str) and tool_call_id:
                            tool_result_ids.add(tool_call_id)
                        text = _native_message_text(message)
                        tool_output_marker_count += text.count(tool_marker) if tool_marker else 0

        if kind == response_source_kind and raw_source == response_source_path:
            provider_response_source_bound = True
            terminal_events = [event for event in events if event.get("type") == "agent_end" and _is_terminal_agent_end(event)]
            provider_response_terminal = provider_response_terminal or bool(terminal_events)
            provider_response_terminal_shape = provider_response_terminal_shape or any(
                event.get("type") == "agent_end"
                and (isinstance(event.get("isTerminal"), bool) or isinstance(event.get("willContinue"), bool))
                for event in events
            )

    boundary = _read_json(root / "console-boundary-receipt.json") or {}
    flush = _read_json(root / "transcript-flush-receipt.json") or {}
    stream_drained = (
        boundary.get("claim_state") == "terminal"
        and boundary.get("claim_terminal_state") == "run_completed"
        and flush.get("status") == "pass"
        and flush.get("exit_code") == 0
    )
    linked_tool_call_ids = sorted(tool_call_ids & tool_result_ids)
    return {
        "status": (
            "pass"
            if provider_response_terminal
            and provider_response_terminal_shape
            and provider_response_source_bound
            and stream_drained
            and native_archive_bound
            else "fail"
        ),
        "agent_end_terminal": provider_response_terminal,
        "agent_end_evidence_shape": provider_response_terminal_shape,
        "agent_end_evidence_source": (
            "omp_print_projection" if provider_response_terminal and response_source_kind == "stdout_path" else None
        ),
        "agent_settled_seen": agent_settled_seen,
        "agent_settled_is_not_completion_contract": True,
        "stream_drained": stream_drained,
        "provider_response_source_bound": provider_response_source_bound,
        "provider_response_source_kind": response_source_kind,
        "provider_response_source_path": response_source_path,
        "native_archive_bound": native_archive_bound,
        "native_session_id_bound": native_session_id_bound,
        "native_terminal_after_assistant": False,
        "native_marker_count": native_marker_count,
        "expected_native_session_id": expected_native_id,
        "native_tool_call_ids": sorted(tool_call_ids),
        "native_tool_result_ids": sorted(tool_result_ids),
        "native_linked_tool_call_ids": linked_tool_call_ids,
        "native_tool_output_marker_count": tool_output_marker_count,
        "native_tool_evidence_complete": (bool(linked_tool_call_ids) and tool_output_marker_count == 1),
        "malformed_source": malformed_source,
        "source_paths": source_paths,
    }


def omp_console_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    settlement = observation.get("omp_settlement")
    settlement = settlement if isinstance(settlement, Mapping) else {}
    return {
        ASSERTION_ID: all(
            (
                observation.get("runtime_host_turn_dispatch") is True,
                observation.get("exact_session_thread_run_binding") is True,
                observation.get("transcript_converged_exactly_once") is True,
                settlement.get("agent_end_terminal") is True,
                settlement.get("agent_end_evidence_shape") is True,
                settlement.get("provider_response_source_bound") is True,
                settlement.get("provider_response_source_kind") == "stdout_path",
                settlement.get("stream_drained") is True,
                settlement.get("native_archive_bound") is True,
                settlement.get("native_session_id_bound") is True,
                settlement.get("native_marker_count") == 1,
                settlement.get("native_tool_evidence_complete") is True,
                settlement.get("malformed_source") is False,
                observation.get("omp_continuation_context_recalled") is True,
                observation.get("interrupt_contract_preserved") is True,
                observation.get("post_interrupt_sendable") is True,
                observation.get("no_orphan_provider_processes") is True,
                observation.get("canary_session_hidden") is True,
            )
        )
    }


def run_omp_console(args: argparse.Namespace) -> dict[str, object]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    generic = lifecycle._run_live("omp", SUPPORTED_VARIANT, args, root)
    observation = dict(generic.get("observation") or {})
    observation["runtime_host_turn_dispatch"] = observation.get("adapter_dispatch_started") is True
    dispatch = _read_json(root / "adapter-dispatch-receipt.json") or {}
    cleanup = _read_json(root / "cleanup-receipt.json") or {}
    session_id = str(dispatch.get("session_id") or "")
    session_retirement = cleanup.get("session_retirement")
    exact_retirement = _exact_session_retirement(session_retirement, session_id)
    cleanup["session_retirement_exact"] = exact_retirement
    cleanup["canary_session_hidden"] = exact_retirement
    if not exact_retirement:
        cleanup["status"] = "fail"
    lifecycle.write_json(root / "cleanup-receipt.json", cleanup)
    observation["canary_session_hidden"] = exact_retirement
    settlement = _native_settlement(root)
    observation["omp_settlement"] = settlement
    observation.update(
        {
            "omp_agent_end_terminal_observed": settlement.get("agent_end_terminal") is True,
            "omp_stream_drained": settlement.get("stream_drained") is True,
            "omp_native_archive_bound": settlement.get("native_archive_bound") is True,
            "omp_continuation_context_recalled": observation.get("continuation_context_recalled") is True,
        }
    )
    lifecycle.write_json(root / "omp-settlement-receipt.json", settlement)
    assertions = omp_console_assertions(observation)
    result = {
        "schema_version": 1,
        "artifact_kind": "omp_console_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "omp",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        "status": "pass" if generic.get("status") == "pass" and all(assertions.values()) else "fail",
        "assertions": assertions,
        "provider_binary": generic.get("provider_binary"),
        "observation": observation,
        "generic_lifecycle_result": generic,
        "artifact_manifest": artifact_manifest(root),
    }
    lifecycle.write_json(root / "result.json", result)
    return result


def _request_args(request_path: Path, output_root: Path) -> argparse.Namespace:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("OMP qualification request must be an object")
    engine = Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or "")
    cli = Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse")
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        engine=engine,
        longhouse_cli=cli,
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        provider="omp",
        variant=SUPPORTED_VARIANT,
        model=os.environ.get("LONGHOUSE_OMP_QUALIFICATION_MODEL", ""),
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    identity.load_request(
        request_path,
        provider="omp",
        profile=PROFILE,
        version_grammar=_PROFILE.version_grammar,
    )

    def execute(binary: Path, evidence_root: Path):
        args = _request_args(request_path, evidence_root)
        args.provider_bin = binary
        result = run_omp_console(args)
        observation = dict(result.get("observation") or {})
        passed = result.get("status") == "pass"
        assertion = semantic.SemanticAssertion(
            ASSERTION_ID,
            AssertionOutcome.PASS if passed else AssertionOutcome.SEMANTIC_FAIL,
            EvidenceClass.LIVE_TOKEN,
        )
        return (
            observation,
            (assertion,),
            tuple(value for name in ("OPENROUTER_API_KEY",) if (value := str(os.environ.get(name) or "").strip())),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=(ASSERTION_ID,),
        executor=execute,
        oracle_source=Path(__file__),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=(_VARIANT,))
    parser.add_argument("--model", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    try:
        result = run_omp_console(args)
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        result = {
            "schema_version": 1,
            "artifact_kind": "omp_console_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "omp",
            "variant": SUPPORTED_VARIANT,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "omp_console_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lifecycle.write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
