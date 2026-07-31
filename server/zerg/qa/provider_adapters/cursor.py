from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.qa.universal_agent_harness import STATUS_FAIL
from zerg.qa.universal_agent_harness import STATUS_PASS
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import register_adapter

CURSOR_GATE0_ARTIFACT_ENV = "LONGHOUSE_CURSOR_GATE0_ARTIFACT"


@register_adapter("cursor")
class CursorHarnessAdapter(UniversalProviderAdapter):
    """Cursor concrete adapter for the universal Longhouse action contract."""

    def _gate0_result(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        required_scenarios: tuple[str, ...],
        operations: tuple[str, ...],
    ) -> dict[str, Any] | None:
        artifact_value = str(os.environ.get(CURSOR_GATE0_ARTIFACT_ENV) or "").strip()
        if not artifact_value:
            return None
        artifact_path = Path(artifact_value).expanduser()
        try:
            artifact: Any = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload = {
                "status": STATUS_FAIL,
                "scenario": scenario,
                "failure_code": "cursor_gate0_artifact_invalid",
                "message": f"{type(exc).__name__}: {exc}",
            }
            package.write_json(f"assertions/{scenario}.json", payload)
            return payload
        if not isinstance(artifact, dict) or artifact.get("provider") != "cursor":
            payload = {
                "status": STATUS_FAIL,
                "scenario": scenario,
                "failure_code": "cursor_gate0_artifact_invalid",
                "message": "Cursor Gate 0 artifact must be an object for provider=cursor.",
            }
            package.write_json(f"assertions/{scenario}.json", payload)
            return payload

        probe = self.probe(package)
        probe_version = str(probe.get("version") or "").strip()
        gate_version = str(artifact.get("provider_version") or "").strip()
        binary, binary_error = self._require_binary(package, scenario)
        if binary_error is not None:
            return binary_error
        assert binary is not None
        resolved_binary = binary.expanduser().resolve(strict=True)
        digest = hashlib.sha256()
        with resolved_binary.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        executable_identity = f"sha256:{digest.hexdigest()}"
        gate_identity = str(artifact.get("provider_executable_identity") or "").strip()
        scenario_map = artifact.get("scenarios")
        scenario_map = scenario_map if isinstance(scenario_map, Mapping) else {}
        failed_scenarios = {
            name: str((scenario_map.get(name) or {}).get("status") or "missing")
            for name in required_scenarios
            if not isinstance(scenario_map.get(name), Mapping) or str((scenario_map.get(name) or {}).get("status") or "") != "passed"
        }
        passed = (
            artifact.get("status") == "passed"
            and probe.get("status") == STATUS_PASS
            and bool(probe_version)
            and gate_version == probe_version
            and gate_identity == executable_identity
            and not failed_scenarios
        )
        evidence = {
            operation: {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "live_token",
                "canary": "cursor_helm_gate0",
                "failure_code": None if passed else "cursor_gate0_contract_failed",
            }
            for operation in operations
        }
        identity = scenario_map.get(required_scenarios[0]) if required_scenarios else {}
        identity = identity if isinstance(identity, Mapping) else {}
        provider_session_id = str(
            identity.get("provider_conversation_id") or identity.get("longhouse_session_id") or self._session_id(package)
        )
        raw_events = [
            {
                "type": "session_start",
                "role": "system",
                "text": f"Cursor Gate 0 observed {', '.join(required_scenarios)}.",
                "provider_session_id": provider_session_id,
                "source_canary": "cursor_helm_gate0",
                "provider_version": gate_version,
                "evidence_origin": "provider_live_canary",
            }
        ]
        projection, evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )
        if db_ingest.get("status") != STATUS_PASS:
            passed = False
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": scenario,
            "provider_version": gate_version,
            "gate0_artifact_path": str(artifact_path.resolve(strict=False)),
            "required_gate0_scenarios": list(required_scenarios),
            "failed_gate0_scenarios": failed_scenarios,
            "synthetic": False,
            "operation_evidence": evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if gate_version != probe_version:
            payload["failure_code"] = "cursor_gate0_version_mismatch"
            payload["message"] = f"Gate 0 proved {gate_version!r}; declared binary reports {probe_version!r}."
        elif gate_identity != executable_identity:
            payload["failure_code"] = "cursor_gate0_identity_mismatch"
            payload["message"] = "Gate 0 did not prove the exact declared Cursor executable identity."
        elif artifact.get("status") != "passed":
            payload["failure_code"] = str(artifact.get("failure_code") or "cursor_gate0_failed")
            payload["message"] = str(artifact.get("error") or "Cursor Gate 0 did not complete successfully.")
        elif failed_scenarios:
            payload["failure_code"] = "cursor_gate0_contract_failed"
            payload["message"] = "Cursor Gate 0 did not pass every scenario required by this harness scenario."
        elif db_ingest.get("status") != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "cursor_gate0_db_ingest_failed"
            payload["message"] = "Cursor Gate 0 evidence did not pass Longhouse DB ingest assertions."
        package.write_json(f"assertions/{scenario}.json", payload)
        return payload

    def launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
        return self._gate0_result(
            package,
            scenario="launch_managed_session",
            required_scenarios=("workspace_trust", "create_chat_resume"),
            operations=("launch_local",),
        ) or super().launch_managed_session(package)

    def send_receive(self, package: EvidencePackage, prompt: str) -> dict[str, Any]:
        package.write_text("input/prompt.txt", prompt)
        return self._gate0_result(
            package,
            scenario="send_receive",
            required_scenarios=("create_chat_resume",),
            operations=("send_input", "transcript_binding"),
        ) or super().send_receive(package, prompt)

    def interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        return self._gate0_result(
            package,
            scenario="interrupt_cancel",
            required_scenarios=("ctrl_c_cancel",),
            operations=("interrupt",),
        ) or super().interrupt_cancel(package)

    def resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        return self._gate0_result(
            package,
            scenario="resume_reattach",
            required_scenarios=("native_resume_continuity",),
            operations=("reattach",),
        ) or super().resume_reattach(package)

    def permission_prompt(self, package: EvidencePackage) -> dict[str, Any]:
        return self._gate0_result(
            package,
            scenario="permission_prompt",
            required_scenarios=("permission_allow", "permission_deny", "permission_ask"),
            operations=("permission_prompt",),
        ) or super().permission_prompt(package)

    def managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
        return self._gate0_result(
            package,
            scenario="managed_session_e2e",
            required_scenarios=(
                "create_chat_resume",
                "native_resume_continuity",
                "ctrl_c_cancel",
                "permission_allow",
                "permission_deny",
                "permission_ask",
            ),
            operations=(
                "launch_local",
                "send_input",
                "reattach",
                "interrupt",
                "permission_prompt",
                "transcript_binding",
            ),
        ) or super().managed_session_e2e(package)

    def live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        return self._gate0_result(
            package,
            scenario="live_token_streaming",
            required_scenarios=("create_chat_resume",),
            operations=("send_input", "live_token_behavior"),
        ) or super().live_token_streaming(package)

    def conversation_reset(self, package: EvidencePackage) -> dict[str, Any]:
        from zerg.qa.conversation_reset import consume_live_reset_artifact

        return consume_live_reset_artifact(self, package, provider="cursor") or super().conversation_reset(package)
