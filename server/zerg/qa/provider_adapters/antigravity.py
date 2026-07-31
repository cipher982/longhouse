"""Antigravity adapter and evidence normalization for the universal provider harness."""

from __future__ import annotations

from typing import Any
from typing import Mapping

from zerg.qa.universal_agent_harness import STATUS_FAIL
from zerg.qa.universal_agent_harness import STATUS_PASS
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import _uniform_operation_evidence
from zerg.qa.universal_agent_harness import register_adapter
from zerg.qa.universal_agent_harness import run_provider_control_e2e_canary


def _antigravity_control_canary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    canary = dict(dict(artifact.get("canaries") or {}).get("antigravity") or {})
    return canary


def antigravity_provider_live_raw_events(
    artifact: Mapping[str, Any],
    *,
    provider_session_id: str,
) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    binary_identity = dict(canaries.get("binary_identity") or {})
    command_shape = dict(canaries.get("command_shape") or {})
    plugin_contract = dict(canaries.get("plugin_contract") or {})
    global_hooks = dict(canaries.get("global_hooks_contract") or {})
    hook_inbox = dict(canaries.get("hook_inbox_claim_contract") or {})
    provider_version = artifact.get("provider_version") or binary_identity.get("version")
    rows: list[dict[str, Any]] = []
    if binary_identity:
        rows.append(
            {
                "type": "session_start",
                "role": "system",
                "text": f"Antigravity binary identity captured: {provider_version}",
                "provider_session_id": provider_session_id,
                "source_canary": "binary_identity",
                "status": binary_identity.get("status"),
                "provider_version": provider_version,
                "evidence_origin": "provider_live_canary",
            }
        )
    if command_shape:
        rows.append(
            {
                "type": "launch_contract",
                "role": "system",
                "text": "Antigravity CLI/plugin command contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "command_shape",
                "status": command_shape.get("status"),
                "missing_by_probe": command_shape.get("missing_by_probe"),
                "failure_code": command_shape.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if plugin_contract:
        rows.append(
            {
                "type": "launch_contract",
                "role": "system",
                "text": "Antigravity Longhouse runtime plugin validate/install/list contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "plugin_contract",
                "status": plugin_contract.get("status"),
                "plugin_root": plugin_contract.get("plugin_root"),
                "isolated_home": plugin_contract.get("isolated_home"),
                "failure_code": plugin_contract.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if global_hooks:
        rows.append(
            {
                "type": "external_event_channel",
                "role": "system",
                "text": "Antigravity global hooks config contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "global_hooks_contract",
                "status": global_hooks.get("status"),
                "events": global_hooks.get("events"),
                "global_hooks_path": global_hooks.get("global_hooks_path"),
                "failure_code": global_hooks.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if hook_inbox:
        rows.append(
            {
                "type": "external_event_channel",
                "role": "system",
                "text": "Antigravity hook-inbox claim contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "hook_inbox_claim_contract",
                "status": hook_inbox.get("status"),
                "pre_claim_event": hook_inbox.get("pre_claim_event"),
                "post_claim_event": hook_inbox.get("post_claim_event"),
                "stop_decision": hook_inbox.get("stop_decision"),
                "failure_code": hook_inbox.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    return rows


def antigravity_real_send_raw_events(canary: Mapping[str, Any]) -> list[dict[str, Any]]:
    session_id = str(canary.get("session_id") or "antigravity-real-agy-send")
    marker = str(canary.get("marker") or "marker-unavailable")
    queued_text = str(canary.get("queued_text") or f"Reply exactly {marker}")
    matching_claim = canary.get("matching_claim")
    matching_claim = matching_claim if isinstance(matching_claim, Mapping) else {}
    rows: list[dict[str, Any]] = []
    if canary:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": queued_text,
                "provider_session_id": session_id,
                "source_canary": "antigravity_real_agy_send",
                "hook_event": matching_claim.get("hook_event"),
                "claim_id": matching_claim.get("id"),
                "conversation_id": matching_claim.get("conversation_id"),
                "marker": marker,
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": marker if canary.get("marker_in_stdout") else "",
                "provider_session_id": session_id,
                "source_canary": "antigravity_real_agy_send",
                "marker_in_stdout": canary.get("marker_in_stdout"),
                "baseline_in_stdout": canary.get("baseline_in_stdout"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    return rows


def antigravity_real_send_operation_evidence(canary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return _uniform_operation_evidence(
        passed=canary.get("status") == "pass",
        level="live_token",
        canary="antigravity_real_agy_send",
        default_failure_code="antigravity_real_agy_send_failed",
        operations=("send_input", "live_token_behavior"),
        raw_failure_code=canary.get("failure_code"),
        seed=canary.get("operation_evidence"),
    )


def antigravity_control_raw_events(canary: Mapping[str, Any]) -> list[dict[str, Any]]:
    session_id = str(canary.get("session_id") or "antigravity-hook-inbox-e2e")
    rows: list[dict[str, Any]] = []
    rows.append(
        {
            "type": "session_start",
            "role": "system",
            "text": "Antigravity hook inbox session observed by provider-control canary.",
            "provider_session_id": session_id,
            "source_canary": "antigravity_hook_inbox",
            "status": canary.get("status"),
            "evidence_origin": "provider_control_e2e_canary",
        }
    )
    pre = canary.get("pre_injection")
    if isinstance(pre, Mapping):
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": "pre invocation canary input",
                "provider_session_id": session_id,
                "source_canary": "antigravity_pre_injection",
                "inject_steps": pre.get("injectSteps"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    post = canary.get("post_injection")
    if isinstance(post, Mapping):
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": "post invocation canary input",
                "provider_session_id": session_id,
                "source_canary": "antigravity_post_injection",
                "termination_behavior": post.get("terminationBehavior"),
                "inject_steps": post.get("injectSteps"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    stop = canary.get("stop_decision")
    if isinstance(stop, Mapping):
        rows.append(
            {
                "type": "runtime_phase",
                "role": "system",
                "text": f"Antigravity Stop hook decision: {stop.get('decision')}",
                "provider_session_id": session_id,
                "source_canary": "antigravity_stop_decision",
                "decision": stop.get("decision"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    return rows


def antigravity_control_operation_evidence(canary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    status = STATUS_PASS if canary.get("status") == "pass" else STATUS_FAIL
    failure_code = None if status == STATUS_PASS else str(canary.get("failure_code") or "antigravity_hook_inbox_failed")
    return {
        "external_event_channel": {
            "status": status,
            "level": "hermetic",
            "canary": "provider_control_e2e_antigravity_hook_inbox",
            "failure_code": failure_code,
        },
        "send_input": {
            "status": status,
            "level": "hermetic",
            "canary": "provider_control_e2e_antigravity_hook_inbox",
            "failure_code": failure_code,
        },
        "runtime_phase": {
            "status": status,
            "level": "hermetic",
            "canary": "provider_control_e2e_antigravity_hook_inbox",
            "failure_code": failure_code,
        },
    }


@register_adapter("antigravity")
class AntigravityHarnessAdapter(UniversalProviderAdapter):
    """Antigravity concrete adapter for the universal Longhouse action contract.

    Phase 3 of docs/specs/provider-factory-coherence.md ("split the
    adapter"): first extraction slice, per Hatch Sol design review
    2026-07-28. Overrides live here instead of branching inside the shared
    base class; the base class no longer special-cases "antigravity" for
    any of these five methods.
    """

    def conversation_reset(self, package: EvidencePackage) -> dict[str, Any]:
        from zerg.qa.conversation_reset import consume_live_reset_artifact

        return consume_live_reset_artifact(self, package, provider="antigravity") or super().conversation_reset(package)

    def launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "launch_managed_session")
        if binary_error is not None:
            return binary_error

        from zerg.qa.provider_live_canary import run_provider_live_canary

        live_evidence_root = package.path("raw", "provider-live-evidence")
        live_artifact_path = package.path("raw", "provider-live-canary.json")
        live_artifact = run_provider_live_canary(
            {
                "provider": "antigravity",
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
        session_projection_data = dict(live_artifact.get("session_projection") or {})
        provider_session_id = str(session_projection_data.get("provider_session_id") or self._session_id(package))
        raw_events = antigravity_provider_live_raw_events(live_artifact, provider_session_id=provider_session_id)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        live_verdict = str(live_artifact.get("verdict") or "red")
        db_verdict = str(db_ingest.get("status") or STATUS_FAIL)
        status = STATUS_PASS if live_verdict == "green" and db_verdict == STATUS_PASS else STATUS_FAIL
        payload = {
            **projection,
            "status": status,
            "scenario": "launch_managed_session",
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        launch_status = str((operation_evidence.get("launch_local") or {}).get("status") or STATUS_FAIL)
        if live_verdict != "green":
            payload["failure_code"] = live_artifact.get("failure_code") or "provider_live_canary_failed"
            payload["message"] = "Antigravity provider-live no-token canary did not pass."
        elif db_verdict != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "launch_managed_session_db_ingest_failed"
            payload["message"] = "Antigravity provider-live evidence did not pass Longhouse DB ingest assertions."
        elif launch_status != STATUS_PASS:
            payload["status"] = STATUS_FAIL
            payload["failure_code"] = "antigravity_launch_local_evidence_missing"
            payload["message"] = "Antigravity provider-live canary did not produce passing launch_local evidence."
        package.write_json("assertions/launch_managed_session.json", payload)
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
        return self._run_antigravity_managed_session_e2e(package)

    def external_event_channel(self, package: EvidencePackage) -> dict[str, Any]:
        payload = dict(self._run_antigravity_managed_session_e2e(package))
        operation_evidence = {
            str(operation): dict(evidence)
            for operation, evidence in dict(payload.get("operation_evidence") or {}).items()
            if isinstance(evidence, Mapping)
        }
        external_status = str((operation_evidence.get("external_event_channel") or {}).get("status") or STATUS_FAIL)
        db_status = str(((payload.get("longhouse_ingest") or {}).get("status")) or STATUS_FAIL)
        passed = external_status == STATUS_PASS and db_status == STATUS_PASS
        payload["status"] = STATUS_PASS if passed else STATUS_FAIL
        payload["scenario"] = "external_event_channel"
        if passed:
            payload.pop("failure_code", None)
            payload.pop("message", None)
        else:
            payload["failure_code"] = payload.get("failure_code") or "external_event_channel_failed"
            payload["message"] = "Antigravity hook/inbox external-event canary did not pass."
        package.write_json("assertions/external_event_channel.json", payload)
        return payload

    def permission_prompt(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._unsupported_payload(
            "permission_prompt",
            "permission_prompt_unsupported",
            "Antigravity does not expose stable provider permission-prompt approve/deny semantics.",
        )
        payload["operation_evidence"] = {
            "permission_prompt": {
                "status": payload["status"],
                "level": "none",
                "canary": "universal_permission_prompt",
                "failure_code": "permission_prompt_unsupported",
            }
        }
        package.write_json("assertions/permission_prompt.json", payload)
        return payload

    def live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "live_token_streaming")
        if binary_error is not None:
            return binary_error

        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="antigravity",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
            extra_args=["--antigravity-real-agy-send"],
            extra_env={"LONGHOUSE_ANTIGRAVITY_BIN": str(binary)},
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        antigravity = _antigravity_control_canary(control_artifact)
        operation_evidence = antigravity_real_send_operation_evidence(antigravity)
        raw_events = antigravity_real_send_raw_events(antigravity)
        provider_session_id = str(antigravity.get("session_id") or self._session_id(package))
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(control_artifact.get("verdict") or "red")
        send_status = str((operation_evidence.get("send_input") or {}).get("status") or STATUS_FAIL)
        live_status = str((operation_evidence.get("live_token_behavior") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = all(
            (
                verdict == "green",
                send_status == STATUS_PASS,
                live_status == STATUS_PASS,
                db_status == STATUS_PASS,
            )
        )
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "live_token_streaming",
            "provider_version": antigravity.get("provider_version"),
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or send_status != STATUS_PASS or live_status != STATUS_PASS:
            failure_code = control_artifact.get("failure_code") or antigravity.get("failure_code")
            payload["failure_code"] = failure_code or "antigravity_live_token_streaming_failed"
            payload["message"] = "Antigravity real-agy send canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "live_token_streaming_db_ingest_failed"
            payload["message"] = "Antigravity live-token evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload

    def _run_antigravity_managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="antigravity",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        antigravity = dict(dict(control_artifact.get("canaries") or {}).get("antigravity") or {})
        raw_events = antigravity_control_raw_events(antigravity)
        provider_session_id = str(antigravity.get("session_id") or self._session_id(package))
        operation_evidence = antigravity_control_operation_evidence(antigravity)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(control_artifact.get("verdict") or "red")
        canary_status = str(antigravity.get("status") or "fail")
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and canary_status == "pass" and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "managed_session_e2e",
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if canary_status != "pass":
            payload["failure_code"] = antigravity.get("failure_code") or control_artifact.get("failure_code")
            payload["message"] = "Antigravity hook/inbox provider-control canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "managed_session_e2e_db_ingest_failed"
            payload["message"] = "Antigravity hook/inbox evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/managed_session_e2e.json", payload)
        return payload
