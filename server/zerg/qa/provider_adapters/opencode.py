"""OpenCode adapter and evidence normalization for the universal provider harness."""

from __future__ import annotations

import json
from typing import Any
from typing import Mapping

from zerg.qa.universal_agent_harness import STATUS_BLOCKED
from zerg.qa.universal_agent_harness import STATUS_FAIL
from zerg.qa.universal_agent_harness import STATUS_PASS
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import _clean_optional_str
from zerg.qa.universal_agent_harness import _uniform_operation_evidence
from zerg.qa.universal_agent_harness import ingest_canonical_events_into_longhouse_db
from zerg.qa.universal_agent_harness import register_adapter
from zerg.qa.universal_agent_harness import run_provider_control_e2e_canary


def _opencode_control_canary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    canary = dict(dict(artifact.get("canaries") or {}).get("opencode") or {})
    return canary


def _first_opencode_control_session_id(artifact: Mapping[str, Any]) -> str | None:
    canary = _opencode_control_canary(artifact)
    session_ids = canary.get("session_ids")
    if isinstance(session_ids, list):
        for session_id in session_ids:
            cleaned = _clean_optional_str(session_id)
            if cleaned:
                return cleaned
    tool_event = canary.get("matching_tool_event")
    if isinstance(tool_event, Mapping):
        return _clean_optional_str(tool_event.get("sessionID"))
    done_event = canary.get("done_text_event")
    if isinstance(done_event, Mapping):
        return _clean_optional_str(done_event.get("sessionID"))
    text_event = canary.get("matching_text_event")
    if isinstance(text_event, Mapping):
        return _clean_optional_str(text_event.get("sessionID"))
    return None


def opencode_provider_live_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    session_create = dict(canaries.get("session_create") or {})
    prompt_async = dict(canaries.get("prompt_async_no_reply_delivery") or {})
    reattach = dict(canaries.get("process_restart_reattach_contract") or {})
    abort = dict(canaries.get("session_abort") or {})
    provider_session_id = str(
        session_create.get("provider_session_id")
        or prompt_async.get("provider_session_id")
        or reattach.get("provider_session_id")
        or abort.get("provider_session_id")
        or ""
    )
    rows: list[dict[str, Any]] = []
    if session_create:
        rows.append(
            {
                "type": "session_start",
                "role": "system",
                "text": "OpenCode server bridge created a provider session.",
                "provider_session_id": provider_session_id,
                "source_canary": "session_create",
                "tokens": session_create.get("tokens"),
                "cost": session_create.get("cost"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if prompt_async:
        marker_sha = prompt_async.get("message_marker_sha256")
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": f"OpenCode prompt_async noReply marker sha256:{marker_sha}",
                "provider_session_id": provider_session_id,
                "source_canary": "prompt_async_no_reply_delivery",
                "message_marker_sha256": marker_sha,
                "observed_message_count": prompt_async.get("observed_message_count"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if reattach:
        rows.append(
            {
                "type": "session_reattach",
                "role": "system",
                "text": "OpenCode restarted server recovered the provider session and marker transcript.",
                "provider_session_id": provider_session_id,
                "source_canary": "process_restart_reattach_contract",
                "message_marker_sha256": reattach.get("message_marker_sha256"),
                "observed_message_count": reattach.get("observed_message_count"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if abort:
        rows.append(
            {
                "type": "interrupt",
                "role": "system",
                "text": "OpenCode session.abort accepted a request against the provider session.",
                "provider_session_id": provider_session_id,
                "source_canary": "session_abort",
                "evidence_origin": "provider_live_canary",
            }
        )
    return rows


def opencode_tool_call_result_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    tool = _opencode_control_canary(artifact)
    provider_session_id = _first_opencode_control_session_id(artifact) or "opencode-tool-call-result"
    matching_event = tool.get("matching_tool_event")
    matching_event = matching_event if isinstance(matching_event, Mapping) else {}
    done_event = tool.get("done_text_event")
    done_event = done_event if isinstance(done_event, Mapping) else {}
    marker = str(tool.get("marker") or "marker-unavailable")
    tool_call_id = str(tool.get("tool_call_id") or "opencode-real-tool-result-shape")
    tool_name = str(tool.get("tool_name") or matching_event.get("tool") or "bash")
    command = f"printf '{marker}'"
    output = marker if matching_event.get("output_exact_match") or tool.get("status") == STATUS_PASS else ""
    rows: list[dict[str, Any]] = []
    if tool:
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": "OpenCode requested a shell command through the real-tool canary.",
                "provider_session_id": provider_session_id,
                "source_canary": "opencode_real_tool_result_shape",
                "tool_name": tool_name,
                "tool_input_json": {"command": command},
                "tool_call_id": tool_call_id,
                "command_status": tool.get("tool_state_status") or matching_event.get("state_status"),
                "command_exact_match": matching_event.get("command_exact_match"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "tool",
                "role": "tool",
                "text": output,
                "provider_session_id": provider_session_id,
                "source_canary": "opencode_real_tool_result_shape",
                "tool_name": tool_name,
                "tool_output_text": output,
                "tool_call_id": tool_call_id,
                "output_exact_match": matching_event.get("output_exact_match"),
                "metadata_output_exact_match": matching_event.get("metadata_output_exact_match"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    if done_event or tool.get("status") == STATUS_PASS:
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": "DONE",
                "provider_session_id": provider_session_id,
                "source_canary": "opencode_real_tool_result_shape",
                "text_exact_match": done_event.get("text_exact_match"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    return rows


def opencode_real_print_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canary = _opencode_control_canary(artifact)
    provider_session_id = _first_opencode_control_session_id(artifact) or "opencode-real-print"
    marker = str(canary.get("marker") or "marker-unavailable")
    prompt = f"Reply with exactly {marker} and nothing else."
    matching_text_event = canary.get("matching_text_event")
    matching_text_event = matching_text_event if isinstance(matching_text_event, Mapping) else {}
    exact_match = bool(matching_text_event.get("text_exact_match"))
    rows: list[dict[str, Any]] = []
    if canary:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": prompt,
                "provider_session_id": provider_session_id,
                "source_canary": "opencode_real_print",
                "marker": marker,
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": marker if exact_match else "",
                "provider_session_id": provider_session_id,
                "source_canary": "opencode_real_print",
                "text_exact_match": exact_match,
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    return rows


def opencode_tool_call_result_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    tool = _opencode_control_canary(artifact)
    return _uniform_operation_evidence(
        passed=tool.get("status") == "pass",
        level="live_token",
        canary="opencode_real_tool_result_shape",
        default_failure_code="opencode_tool_call_result_failed",
        operations=("tool_call_result",),
        raw_failure_code=tool.get("failure_code"),
        seed=tool.get("operation_evidence"),
    )


def opencode_real_print_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    canary = _opencode_control_canary(artifact)
    return _uniform_operation_evidence(
        passed=canary.get("status") == "pass",
        level="live_token",
        canary="opencode_real_print",
        default_failure_code="opencode_real_print_failed",
        operations=("run_once", "live_token_behavior"),
        raw_failure_code=canary.get("failure_code"),
        seed=canary.get("operation_evidence"),
    )


def opencode_real_print_model_evidence(artifact: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one canonical model-backed envelope for receipt derivation."""

    canary = _opencode_control_canary(artifact)
    result_event = canary.get("result_event")
    if not isinstance(result_event, Mapping):
        return None
    model = _clean_optional_str(result_event.get("model") or canary.get("model"))
    return {
        "source_canary": "opencode_real_print",
        "operation_evidence": opencode_real_print_operation_evidence(artifact),
        "model": model,
        "result_event": dict(result_event),
    }


@register_adapter("opencode")
class OpenCodeHarnessAdapter(UniversalProviderAdapter):
    """OpenCode concrete adapter for the universal Longhouse action contract.

    Phase 3 of docs/specs/provider-factory-coherence.md ("split the
    adapter"): second extraction slice (after Antigravity), per Hatch Sol
    design review 2026-07-28.
    """

    def conversation_reset(self, package: EvidencePackage) -> dict[str, Any]:
        from zerg.qa.conversation_reset import consume_live_reset_artifact

        return consume_live_reset_artifact(self, package, provider="opencode") or super().conversation_reset(package)

    def permission_prompt(self, package: EvidencePackage) -> dict[str, Any]:
        payload = {
            "status": STATUS_BLOCKED,
            "scenario": "permission_prompt",
            "failure_code": "opencode_native_permission_canary_required",
            "message": ("The Python permission bridge was retired; this action needs evidence from the native OpenCode control adapter."),
            "operation_evidence": {
                "permission_prompt": {
                    "status": STATUS_BLOCKED,
                    "level": "hermetic",
                    "canary": "opencode_native_permission_canary_required",
                    "failure_code": "opencode_native_permission_canary_required",
                }
            },
            "proof_scope": "opencode_native_permission_canary_required",
        }
        package.write_json("assertions/permission_prompt.json", payload)
        return payload

    def managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
        if not self.config.real_managed_session_e2e:
            payload = self._unsupported_payload(
                "managed_session_e2e",
                "managed_session_e2e_not_migrated",
                "No real no-token managed-session e2e adapter is implemented for this provider yet.",
            )
            package.write_json("assertions/managed_session_e2e.json", payload)
            return payload
        binary, source = self._resolve_binary()
        if binary is None:
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "provider_binary_not_found",
                "message": "opencode binary was not found for managed_session_e2e",
                "binary_source": source,
            }
            package.write_json("assertions/managed_session_e2e.json", payload)
            return payload

        from zerg.qa.provider_live_canary import run_provider_live_canary

        live_evidence_root = package.path("raw", "provider-live-evidence")
        live_artifact_path = package.path("raw", "provider-live-canary.json")
        live_artifact = run_provider_live_canary(
            {
                "provider": "opencode",
                "provider_bin": str(binary),
                "artifact": live_artifact_path,
                "evidence_root": live_evidence_root,
                "wait_ready_secs": 15.0,
                "json": False,
            }
        )
        package.write_json("raw/provider-live-canary-inline.json", live_artifact)
        operation_evidence = {
            str(operation): dict(evidence)
            for operation, evidence in dict(live_artifact.get("operation_evidence") or {}).items()
            if isinstance(evidence, Mapping)
        }
        raw_events = opencode_provider_live_raw_events(live_artifact)
        projection = self._write_session_projection(
            package,
            raw_events=raw_events,
            operations=operation_evidence,
            provider_session_id=str(
                (live_artifact.get("session_projection") or {}).get("provider_session_id") or self._session_id(package)
            ),
        )
        db_ingest = ingest_canonical_events_into_longhouse_db(
            package=package,
            provider=self.config.provider,
            rows=raw_events,
            provider_session_id=str(
                (live_artifact.get("session_projection") or {}).get("provider_session_id") or self._session_id(package)
            ),
        )
        db_operation_evidence = {
            str(operation): dict(evidence)
            for operation, evidence in dict(db_ingest.get("operation_evidence") or {}).items()
            if isinstance(evidence, Mapping)
        }
        operation_evidence.update(db_operation_evidence)
        session_projection_path = package.path("longhouse", "session-projection.json")
        try:
            session_projection = json.loads(session_projection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            session_projection = {}
        if isinstance(session_projection, dict):
            session_projection["operation_statuses"] = operation_evidence
            package.write_json("longhouse/session-projection.json", session_projection)
        live_verdict = str(live_artifact.get("verdict") or "red")
        db_verdict = str(db_ingest.get("status") or STATUS_FAIL)
        payload = {
            **projection,
            "status": STATUS_PASS if live_verdict == "green" and db_verdict == STATUS_PASS else STATUS_FAIL,
            "scenario": "managed_session_e2e",
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if live_verdict != "green":
            payload["failure_code"] = live_artifact.get("failure_code") or "provider_live_canary_failed"
            payload["message"] = "OpenCode provider-live no-token canary did not pass."
        elif db_verdict != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "managed_session_e2e_db_ingest_failed"
            payload["message"] = "OpenCode provider-live evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/managed_session_e2e.json", payload)
        return payload

    def interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "interrupt_cancel")
        if binary_error is not None:
            return binary_error

        from zerg.qa.provider_live_canary import run_provider_live_canary

        live_evidence_root = package.path("raw", "provider-live-evidence")
        live_artifact_path = package.path("raw", "provider-live-canary.json")
        live_artifact = run_provider_live_canary(
            {
                "provider": "opencode",
                "provider_bin": str(binary),
                "artifact": live_artifact_path,
                "evidence_root": live_evidence_root,
                "wait_ready_secs": 15.0,
                "json": False,
            }
        )
        package.write_json("raw/provider-live-canary-inline.json", live_artifact)
        operation_evidence = self._operation_evidence_map(live_artifact.get("operation_evidence"))
        raw_events = opencode_provider_live_raw_events(live_artifact)
        live_session_projection = live_artifact.get("session_projection") or {}
        provider_session_id = str(live_session_projection.get("provider_session_id") or self._session_id(package))
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        live_verdict = str(live_artifact.get("verdict") or "red")
        interrupt_status = str((operation_evidence.get("interrupt") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = live_verdict == "green" and interrupt_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "interrupt_cancel",
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if live_verdict != "green" or interrupt_status != STATUS_PASS:
            payload["failure_code"] = live_artifact.get("failure_code") or "opencode_interrupt_cancel_failed"
            payload["message"] = "OpenCode session.abort canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "interrupt_cancel_db_ingest_failed"
            payload["message"] = "OpenCode interrupt evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        return self._native_resume_proof(
            package,
            scenario="resume_reattach",
            proof_scope="opencode_process_restart_reattach",
        )

    def cold_resume(self, package: EvidencePackage) -> dict[str, Any]:
        return self._native_resume_proof(
            package,
            scenario="helm_cold_resume_native",
            proof_scope="opencode_process_restart_native_attach",
        )

    def _native_resume_proof(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        proof_scope: str,
    ) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, scenario)
        if binary_error is not None:
            return binary_error

        from zerg.qa.provider_live_canary import run_provider_live_canary

        live_evidence_root = package.path("raw", "provider-live-evidence")
        live_artifact_path = package.path("raw", "provider-live-canary.json")
        live_artifact = run_provider_live_canary(
            {
                "provider": "opencode",
                "provider_bin": str(binary),
                "artifact": live_artifact_path,
                "evidence_root": live_evidence_root,
                "wait_ready_secs": 15.0,
                "json": False,
            }
        )
        package.write_json("raw/provider-live-canary-inline.json", live_artifact)
        operation_evidence = {
            str(operation): dict(evidence)
            for operation, evidence in dict(live_artifact.get("operation_evidence") or {}).items()
            if isinstance(evidence, Mapping)
        }
        raw_events = opencode_provider_live_raw_events(live_artifact)
        live_session_projection = live_artifact.get("session_projection") or {}
        provider_session_id = str(live_session_projection.get("provider_session_id") or self._session_id(package))
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        live_verdict = str(live_artifact.get("verdict") or "red")
        reattach_status = str((operation_evidence.get("reattach") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        payload = {
            **projection,
            "status": STATUS_PASS
            if live_verdict == "green" and reattach_status == STATUS_PASS and db_status == STATUS_PASS
            else STATUS_FAIL,
            "scenario": scenario,
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "proof_scope": proof_scope,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if live_verdict != "green" or reattach_status != STATUS_PASS:
            payload["failure_code"] = live_artifact.get("failure_code") or "opencode_resume_reattach_failed"
            payload["message"] = "OpenCode process-restart reattach canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "resume_reattach_db_ingest_failed"
            payload["message"] = "OpenCode reattach evidence did not pass Longhouse DB ingest assertions."
        package.write_json(f"assertions/{scenario}.json", payload)
        return payload

    def tool_call_result(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "tool_call_result")
        if binary_error is not None:
            return binary_error

        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="opencode",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
            extra_args=["--opencode-run-real-tool"],
            extra_env={"LONGHOUSE_OPENCODE_BIN": str(binary)},
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        operation_evidence = opencode_tool_call_result_operation_evidence(control_artifact)
        raw_events = opencode_tool_call_result_raw_events(control_artifact)
        provider_session_id = _first_opencode_control_session_id(control_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(control_artifact.get("verdict") or "red")
        tool_status = str((operation_evidence.get("tool_call_result") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and tool_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "tool_call_result",
            "provider_version": _opencode_control_canary(control_artifact).get("provider_version"),
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or tool_status != STATUS_PASS:
            payload["failure_code"] = (
                control_artifact.get("failure_code")
                or _opencode_control_canary(control_artifact).get("failure_code")
                or "opencode_tool_call_result_failed"
            )
            payload["message"] = "OpenCode real-tool call/result canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "tool_call_result_db_ingest_failed"
            payload["message"] = "OpenCode real-tool call/result evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/tool_call_result.json", payload)
        return payload

    def live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "live_token_streaming")
        if binary_error is not None:
            return binary_error

        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="opencode",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
            extra_args=["--opencode-run-real-print"],
            extra_env={"LONGHOUSE_OPENCODE_BIN": str(binary)},
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        operation_evidence = opencode_real_print_operation_evidence(control_artifact)
        raw_events = opencode_real_print_raw_events(control_artifact)
        provider_session_id = _first_opencode_control_session_id(control_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        opencode = _opencode_control_canary(control_artifact)
        verdict = str(control_artifact.get("verdict") or "red")
        live_status = str((operation_evidence.get("live_token_behavior") or {}).get("status") or STATUS_FAIL)
        run_once_status = str((operation_evidence.get("run_once") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = all(
            (
                verdict == "green",
                live_status == STATUS_PASS,
                run_once_status == STATUS_PASS,
                db_status == STATUS_PASS,
            )
        )
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "live_token_streaming",
            "provider_version": opencode.get("provider_version"),
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if model_evidence := opencode_real_print_model_evidence(control_artifact):
            payload["live_model_evidence"] = model_evidence
        if verdict != "green" or live_status != STATUS_PASS or run_once_status != STATUS_PASS:
            failure_code = control_artifact.get("failure_code") or opencode.get("failure_code")
            payload["failure_code"] = failure_code or "opencode_live_token_streaming_failed"
            payload["message"] = "OpenCode real-print canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "live_token_streaming_db_ingest_failed"
            payload["message"] = "OpenCode live-token evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload
