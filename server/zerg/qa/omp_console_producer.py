#!/usr/bin/env python3
"""Live OMP Console qualification through the native ``omp_print`` adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_event_digest import raw_event_digest
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_interaction_semantics import omp_agent_end_is_terminal

SCENARIO_ID = "omp_console_lifecycle"
ASSERTION_ID = "omp_console_turn_settled_and_bound"
SUPPORTED_VARIANT = lifecycle.SUPPORTED_VARIANT

REGISTRATION = ProducerRegistration(
    producer_id="omp.console_lifecycle.v1",
    producer_revision=8,
    scenario_id=SCENARIO_ID,
    scenario_revision=8,
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
    required_cleanup=(
        "provider_process_dead",
        "process_group_dead",
        "no_orphan_provider_processes",
        "served_run_retired",
        "canary_session_hidden",
    ),
    implementation="server/zerg/qa/omp_console_producer.py",
    oracle_source="server/zerg/qa/omp_console_producer.py",
    oracle_entrypoint="omp_console_assertions",
    executable_module="zerg.qa.omp_console_producer",
    provider_artifact_required=True,
)
_VARIANT = execution_variant_key(provider="omp", assertion_id=ASSERTION_ID, scenario_id=SCENARIO_ID, variant=None)
PROFILE = "omp_print_v1"
_MAX_NATIVE_MODEL_SOURCE_BYTES = 16 * 1024 * 1024
_PROFILE = identity.IdentityProfile(
    provider="omp",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(version_prefix=r"omp/"),
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


def _numeric_usage(value: object, prefix: str = "") -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    usage: dict[str, int | float] = {}
    for key, item in value.items():
        name = f"{prefix}{key}"
        if type(item) in {int, float}:
            usage[name] = item
        elif isinstance(item, Mapping):
            usage.update(_numeric_usage(item, f"{name}."))
    return usage


def _successful_assistant_event(event: Mapping[str, Any]) -> bool:
    message = event.get("message")
    if event.get("type") != "message" or not isinstance(message, Mapping):
        return False
    if message.get("role") != "assistant" or message.get("stopReason") not in {"stop", "length"}:
        return False
    if message.get("errorMessage"):
        return False
    usage = message.get("usage")
    output_tokens = usage.get("output") if isinstance(usage, Mapping) else None
    if type(output_tokens) not in {int, float} or output_tokens <= 0:
        return False
    native_model = event.get("model")
    if not isinstance(native_model, str) or not native_model.strip():
        message_model = message.get("modelId") or message.get("model")
        if not isinstance(message_model, str) or not message_model.strip():
            return False
    return True


def omp_native_model_evidence(
    root: Path,
    *,
    source_canary: str,
    api_key_configured: bool,
    qualification_model: str | None = None,
    first_turn_only: bool = False,
) -> dict[str, Any] | None:
    """Bind OMP's provider-reported model call to one retained JSONL source."""

    evidence_root = root.resolve()
    retention = _read_json(evidence_root / "provider-source-retention.json") or {}
    sources = retention.get("sources") if isinstance(retention.get("sources"), list) else []
    selected_source: Path | None = None
    selected_source_relative: str | None = None
    selected_events: list[Mapping[str, Any]] = []
    selected_event: Mapping[str, Any] | None = None
    selected_event_window: tuple[int, int] | None = None
    if first_turn_only:
        dispatch = _read_json(evidence_root / "adapter-dispatch-receipt.json") or {}
        binding = _read_json(evidence_root / "provider-response-binding-receipt.json") or {}
        expected_native_id = str(binding.get("provider_thread_id") or dispatch.get("provider_thread_id") or "")
        first_turn = _first_turn_source_window(evidence_root, sources, expected_native_id=expected_native_id)
        if first_turn is None:
            first_turn = _first_turn_source_window_from_response_binding(
                evidence_root,
                sources,
                binding=binding,
                expected_native_id=expected_native_id,
            )
        if first_turn is None:
            return None
        selected_source, selected_source_relative, _, first_turn_events, window_end = first_turn
        selected_event_window = (0, window_end)
        assistant_messages = [
            event
            for event in first_turn_events
            if event.get("type") == "message" and isinstance(event.get("message"), Mapping) and event["message"].get("role") == "assistant"
        ]
        assistants = [event for event in assistant_messages if _successful_assistant_event(event)]
        if assistants:
            selected_event = assistants[-1]
            selected_events = assistant_messages
    else:
        for item in sources:
            if not isinstance(item, Mapping) or item.get("retained") is not True or item.get("kind") != "source_path":
                continue
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = evidence_root / candidate
            if candidate.is_symlink():
                continue
            try:
                path = candidate.resolve(strict=True)
                relative_path = path.relative_to(evidence_root).as_posix()
                source_bytes = path.read_bytes()
            except (OSError, ValueError):
                continue
            try:
                events = [
                    value
                    for line in source_bytes.splitlines()
                    if line.strip()
                    for value in [json.loads(line)]
                    if isinstance(value, Mapping)
                ]
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            assistant_messages = [
                event
                for event in events
                if event.get("type") == "message"
                and isinstance(event.get("message"), Mapping)
                and event["message"].get("role") == "assistant"
            ]
            assistants = [event for event in assistant_messages if _successful_assistant_event(event)]
            if assistants:
                selected_source = path
                selected_source_relative = relative_path
                selected_event = assistants[-1]
                selected_events = assistant_messages
    if selected_source is None or selected_source_relative is None or selected_event is None:
        return None

    raw_message = selected_event.get("message")
    message = raw_message if isinstance(raw_message, Mapping) else {}
    usage: dict[str, int | float] = {}
    for event in selected_events:
        event_message = event.get("message")
        if isinstance(event_message, Mapping):
            for key, value in _numeric_usage(event_message.get("usage")).items():
                previous = usage.get(key, 0)
                total = previous + value
                usage[key] = round(total, 12) if isinstance(total, float) else total
    event_model = selected_event.get("model")
    model_source = "provider_event"
    if not isinstance(event_model, str) or not event_model.strip():
        event_model = message.get("modelId") or message.get("model")
        model_source = "provider_event"
    if not isinstance(event_model, str) or not event_model.strip():
        return None
    native_model = event_model.strip()
    message_model = message.get("model")
    if isinstance(message_model, str) and message_model.strip() and message_model.strip() != native_model:
        return None
    requested_model = qualification_model.strip() if isinstance(qualification_model, str) and qualification_model.strip() else None
    if requested_model is not None and native_model != requested_model:
        return None
    model = native_model
    event_digest = f"sha256:{raw_event_digest(selected_event)}"
    source_digest = f"sha256:{hashlib.sha256(selected_source.read_bytes()).hexdigest()}"
    source_artifact: dict[str, Any] = {
        "path": selected_source_relative,
        "sha256": source_digest,
        "kind": "provider_jsonl_stream",
        "event_type": "message",
        "event_sha256": event_digest,
        "native_event_sha256": event_digest,
    }
    if selected_event_window is not None:
        source_artifact["event_window"] = {
            "start_offset": selected_event_window[0],
            "end_offset": selected_event_window[1],
        }
    return {
        "source_canary": source_canary,
        "operation_evidence": {"model_call": {"status": "pass", "level": "live_token"}},
        "model": model,
        "auth": {
            "credential_mode": "api_key",
            "api_key_source": "env",
            "api_key_configured": api_key_configured,
        },
        "result_event": {
            "type": "message",
            "provider": message.get("provider"),
            "model": model,
            "model_source": model_source,
            "usage": usage,
            "total_cost_usd": usage.get("cost.total"),
            "native_event_sha256": event_digest,
        },
        "source_artifacts": [source_artifact],
    }


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
    return omp_agent_end_is_terminal(event)


def _parse_native_events(source_bytes: bytes) -> tuple[list[Mapping[str, Any]], bool]:
    events: list[Mapping[str, Any]] = []
    malformed = False
    for raw_line in source_bytes.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed = True
            continue
        if isinstance(event, Mapping):
            events.append(event)
        else:
            malformed = True
    return events, malformed


def _retained_source_path(root: Path, raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
        return None
    candidate = root / raw_path
    if candidate.is_symlink():
        return None
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path


def _first_turn_source_window(
    root: Path,
    sources: list[object],
    *,
    expected_native_id: str = "",
) -> tuple[Path, str, bytes, list[Mapping[str, Any]], int] | None:
    continuation = _read_json(root / "console-continuation-receipt.json") or {}
    first_turn = continuation.get("first_turn_evidence")
    if not isinstance(first_turn, Mapping):
        return None
    if first_turn.get("source_kind") != "source_path":
        return None

    retained_path = first_turn.get("retained_path")
    retained_source_path = first_turn.get("retained_source_path")
    source_start_offset = first_turn.get("source_start_offset")
    source_end_offset = first_turn.get("source_end_offset")
    source_sha256 = first_turn.get("source_sha256")
    matching_rows = [
        item
        for item in sources
        if isinstance(item, Mapping)
        and item.get("retained") is True
        and item.get("kind") == "source_path"
        and item.get("path") == retained_path
    ]
    if (
        not isinstance(retained_path, str)
        or not retained_path
        or Path(retained_path).is_absolute()
        or not isinstance(retained_source_path, str)
        or not retained_source_path
        or not isinstance(source_sha256, str)
        or type(source_start_offset) is not int
        or source_start_offset != 0
        or type(source_end_offset) is not int
        or source_end_offset <= source_start_offset
        or len(matching_rows) != 1
        or matching_rows[0].get("source") != retained_source_path
        or (expected_native_id and first_turn.get("provider_thread_id") != expected_native_id)
    ):
        return None

    source_path = _retained_source_path(root, retained_path)
    if source_path is None:
        return None
    try:
        source_bytes = source_path.read_bytes()
    except OSError:
        return None
    if source_end_offset > len(source_bytes) or source_bytes[source_end_offset - 1 : source_end_offset] != b"\n":
        return None
    if source_sha256 != f"sha256:{hashlib.sha256(source_bytes).hexdigest()}":
        return None

    _, full_malformed = _parse_native_events(source_bytes)
    if full_malformed:
        return None
    first_turn_events, first_turn_malformed = _parse_native_events(source_bytes[:source_end_offset])
    if first_turn_malformed:
        return None
    relative_path = source_path.relative_to(root.resolve()).as_posix()
    return source_path, relative_path, source_bytes, first_turn_events, source_end_offset


def _first_turn_source_window_from_response_binding(
    root: Path,
    sources: list[object],
    *,
    binding: Mapping[str, Any],
    expected_native_id: str = "",
) -> tuple[Path, str, bytes, list[Mapping[str, Any]], int] | None:
    """Use the bound first provider response when continuation evidence is absent.

    A semantic canary can fail after the provider has already completed its
    first model turn, before the continuation receipt is written. The response
    binding is still an exact, digest-bound source boundary for that turn; use
    it for model-call accounting rather than misclassifying a real provider
    response as an unauthenticated run.
    """

    source_kind = binding.get("provider_response_source_kind")
    source_ref = binding.get("provider_response_source_path")
    source_digest = binding.get("provider_response_source_sha256")
    if source_kind != "source_path" or not isinstance(source_ref, str) or not source_ref or not isinstance(source_digest, str):
        return None
    matching_rows = [
        item
        for item in sources
        if isinstance(item, Mapping)
        and item.get("retained") is True
        and item.get("kind") == source_kind
        and item.get("source") == source_ref
    ]
    if len(matching_rows) != 1:
        return None
    retained_path = matching_rows[0].get("path")
    source_path = _retained_source_path(root, retained_path)
    if source_path is None:
        return None
    try:
        with source_path.open("rb") as stream:
            source_bytes = stream.read(_MAX_NATIVE_MODEL_SOURCE_BYTES + 1)
    except OSError:
        return None
    if len(source_bytes) > _MAX_NATIVE_MODEL_SOURCE_BYTES:
        return None
    if f"sha256:{hashlib.sha256(source_bytes).hexdigest()}" != source_digest:
        return None
    events, malformed = _parse_native_events(source_bytes)
    if malformed or not expected_native_id:
        return None
    if sum(event.get("type") == "session" and event.get("id") == expected_native_id for event in events) != 1:
        return None
    return source_path, source_path.relative_to(root.resolve()).as_posix(), source_bytes, events, len(source_bytes)


def _native_settlement(root: Path) -> dict[str, object]:
    """Bind OMP's print terminal and native archive/tool evidence separately."""

    retention = _read_json(root / "provider-source-retention.json") or {}
    sources = retention.get("sources") if isinstance(retention.get("sources"), list) else []
    dispatch = _read_json(root / "adapter-dispatch-receipt.json") or {}
    binding = _read_json(root / "provider-response-binding-receipt.json") or {}
    expected_native_id = str(binding.get("provider_thread_id") or dispatch.get("provider_thread_id") or "")
    first_turn = _first_turn_source_window(root, sources, expected_native_id=expected_native_id)
    first_turn_events = first_turn[3] if first_turn is not None else []
    first_turn_source_bound = first_turn is not None
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
        source_path = _retained_source_path(root, raw_path)
        if source_path is None:
            continue
        try:
            source_bytes = source_path.read_bytes()
        except OSError:
            continue
        events, source_malformed = _parse_native_events(source_bytes)
        malformed_source = malformed_source or source_malformed
        if any(event.get("type") == "agent_settled" for event in events):
            agent_settled_seen = True

        if kind == response_source_kind and raw_source == response_source_path:
            provider_response_source_bound = True
            terminal_events = [event for event in events if event.get("type") == "agent_end" and _is_terminal_agent_end(event)]
            provider_response_terminal = provider_response_terminal or bool(terminal_events)
            provider_response_terminal_shape = provider_response_terminal_shape or any(
                event.get("type") == "agent_end"
                and (isinstance(event.get("isTerminal"), bool) or isinstance(event.get("willContinue"), bool))
                for event in events
            )

    native_session_id_bound = (
        sum(1 for event in first_turn_events if event.get("type") == "session" and event.get("id") == expected_native_id) == 1
    )
    for event in first_turn_events:
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

    first_turn_headers = [event for event in first_turn_events if event.get("type") == "session" and event.get("id") == expected_native_id]
    first_turn_marker_count = sum(
        _native_message_text(event["message"]).count(marker)
        for event in first_turn_events
        if event.get("type") in {"message", "message_end"}
        and isinstance(event.get("message"), Mapping)
        and event["message"].get("role") == "assistant"
        and marker
    )
    native_archive_bound = (
        first_turn_source_bound and len(first_turn_headers) == 1 and first_turn_marker_count == 1 and not malformed_source
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
            "pass" if provider_response_terminal and provider_response_source_bound and stream_drained and native_archive_bound else "fail"
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
        "native_marker_count": first_turn_marker_count,
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
    model_evidence = observation.get("live_model_evidence")
    model_evidence_ok = (
        isinstance(model_evidence, Mapping)
        and isinstance(model_evidence.get("model"), str)
        and bool(model_evidence.get("model", "").strip())
        and isinstance(model_evidence.get("source_artifacts"), list)
        and bool(model_evidence.get("source_artifacts"))
    )
    return {
        ASSERTION_ID: all(
            (
                model_evidence_ok,
                observation.get("runtime_host_turn_dispatch") is True,
                observation.get("exact_session_thread_run_binding") is True,
                observation.get("transcript_converged_exactly_once") is True,
                settlement.get("agent_end_terminal") is True,
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
                observation.get("served_run_retired") is True,
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
    cleanup["served_run_retired"] = cleanup.get("served_run_retired") is True
    if not exact_retirement or not cleanup["served_run_retired"]:
        cleanup["status"] = "fail"
    lifecycle.write_json(root / "cleanup-receipt.json", cleanup)
    observation["canary_session_hidden"] = exact_retirement
    observation["served_run_retired"] = cleanup["served_run_retired"]
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
    model_evidence = omp_native_model_evidence(
        root,
        source_canary=PROFILE,
        api_key_configured=bool(str(os.environ.get("OPENROUTER_API_KEY") or "").strip()),
        qualification_model=str(getattr(args, "model", "") or ""),
        first_turn_only=True,
    )
    if model_evidence is not None:
        observation["live_model_evidence"] = model_evidence
        result_event = model_evidence.get("result_event")
        dispatch["native_model"] = model_evidence.get("model")
        dispatch["native_provider"] = result_event.get("provider") if isinstance(result_event, Mapping) else None
        dispatch["native_model_evidence"] = dict(result_event) if isinstance(result_event, Mapping) else None
        lifecycle.write_json(root / "adapter-dispatch-receipt.json", dispatch)
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
        scenario_revision=REGISTRATION.scenario_revision,
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
            "observation_scope": "scenario",
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "omp_console_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        result["artifact_manifest"] = artifact_manifest(args.evidence_root)
        lifecycle.write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
