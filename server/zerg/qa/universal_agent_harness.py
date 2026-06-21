"""Universal provider harness for release-proof scenarios.

This module owns the shared adapter/scenario shape. Provider-specific mechanics
stay behind adapters while universal scenarios produce comparable evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import Protocol
from uuid import NAMESPACE_URL
from uuid import uuid5

from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.provider_orchestration_capabilities import provider_orchestration_capabilities
from zerg.qa.repo_root import default_repo_root
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.managed_provider_contracts import managed_provider_names

SCHEMA_VERSION = 1
ARTIFACT_KIND = "universal_agent_harness_run"
SUPPORTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity")
SCENARIOS = (
    "probe_identity",
    "adapter_conformance",
    "collect_raw_evidence",
    "action_matrix",
    "control_surface",
    "full_action_suite",
    "baseline_compare",
    "parse_ingest_project",
    "db_ingest_project",
    "opencode_lineage_projection",
    "orchestration_capability_matrix",
    "session_projection",
    "timeline_projection",
    "run_prompt_once",
    "launch_managed_session",
    "managed_session_e2e",
    "launch_remote_projection",
    "send_receive",
    "steer_active_turn",
    "pause_request_detect",
    "answer_pause_request",
    "interrupt_cancel",
    "tool_call_result_projection",
    "tool_call_result",
    "resume_reattach",
    "terminate_cleanup",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "multi_turn_continuity",
    "external_event_channel",
    "permission_prompt",
    "crash_timeout_cleanup",
    "live_token_streaming",
    "old_new_release_diff",
)

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNSUPPORTED_GAP = "unsupported_gap"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_BLOCKED = "blocked"
STATUS_FLAKY = "flaky"
STATUS_XFAIL_WITH_EXPIRY = "xfail_with_expiry"
STATUSES = (
    STATUS_PASS,
    STATUS_FAIL,
    STATUS_UNSUPPORTED_GAP,
    STATUS_NOT_APPLICABLE,
    STATUS_BLOCKED,
    STATUS_FLAKY,
    STATUS_XFAIL_WITH_EXPIRY,
)
YELLOW_STATUSES = (STATUS_UNSUPPORTED_GAP, STATUS_BLOCKED, STATUS_FLAKY, STATUS_XFAIL_WITH_EXPIRY)

COVERAGE_GAP_PASSED = "passed"
COVERAGE_GAP_PROVIDER_CONTRACT_UNSUPPORTED = "provider_contract_unsupported"
COVERAGE_GAP_NO_TOKEN_SAFETY_GATE = "no_token_safety_gate"
COVERAGE_GAP_MISSING_LIVE_CANARY = "missing_live_canary"
COVERAGE_GAP_MISSING_CREDENTIALS = "missing_credentials"
COVERAGE_GAP_MISSING_COVERAGE = "missing_coverage"
COVERAGE_GAP_NOT_APPLICABLE = "not_applicable"
COVERAGE_GAP_FLAKY = "flaky"
COVERAGE_GAP_XFAIL_WITH_EXPIRY = "xfail_with_expiry"
COVERAGE_GAP_UNEXPECTED_FAILURE = "unexpected_failure"
COVERAGE_GAP_UNKNOWN = "unknown_gap"

MVP_METHODS = (
    "prepare",
    "probe",
    "adapter_conformance",
    "action_result",
    "action_matrix",
    "control_surface",
    "run_prompt",
    "collect_evidence",
    "decode_normalize",
    "db_ingest_project",
    "session_projection",
    "timeline_projection",
    "launch_managed_session",
    "send_receive",
    "managed_session_e2e",
    "steer_active_turn",
    "pause_request_detect",
    "answer_pause_request",
    "interrupt_cancel",
    "tool_call_result",
    "resume_reattach",
    "terminate_cleanup",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "multi_turn_continuity",
    "external_event_channel",
    "permission_prompt",
    "crash_timeout_cleanup",
    "live_token_streaming",
    "baseline_compare",
    "old_new_release_diff",
    "cleanup",
)
MVP_CAPABILITIES = (
    "identity",
    "raw_evidence",
    "canonical_parse",
    "managed_session",
    "message_exchange",
    "provider_safe_e2e",
    "cleanup",
)
PROFILES = ("fixture_replay", "live_no_token")
SAFE_MANAGED_SESSION_SCENARIOS = ("launch_managed_session", "send_receive")
FULL_ACTION_SUITE_SCENARIOS = (
    "probe_identity",
    "adapter_conformance",
    "collect_raw_evidence",
    "parse_ingest_project",
    "db_ingest_project",
    "session_projection",
    "timeline_projection",
    "run_prompt_once",
    "launch_managed_session",
    "managed_session_e2e",
    "launch_remote_projection",
    "send_receive",
    "steer_active_turn",
    "pause_request_detect",
    "answer_pause_request",
    "interrupt_cancel",
    "tool_call_result_projection",
    "resume_reattach",
    "terminate_cleanup",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "multi_turn_continuity",
    "external_event_channel",
    "permission_prompt",
    "crash_timeout_cleanup",
    "baseline_compare",
    "old_new_release_diff",
)
ACTION_EXECUTION_SCENARIO_BY_ID = {
    "provider_identity": ("probe_identity",),
    "launch_local": ("launch_managed_session",),
    "launch_remote": ("launch_remote_projection",),
    "run_once": ("run_prompt_once",),
    "session_identity": ("launch_managed_session", "resume_reattach", "managed_session_e2e"),
    "send_message": ("send_receive", "interrupt_cancel", "managed_session_e2e"),
    "steer_active_turn": ("steer_active_turn",),
    "pause_request_detect": ("pause_request_detect",),
    "answer_pause_request": ("answer_pause_request",),
    "interrupt_cancel": ("interrupt_cancel",),
    "resume_reattach": ("resume_reattach",),
    "terminate_cleanup": ("terminate_cleanup",),
    "tail_output": ("tail_output",),
    "runtime_phase": ("runtime_phase",),
    "transcript_binding": ("transcript_binding",),
    "multi_turn_continuity": ("multi_turn_continuity",),
    "external_event_channel": ("external_event_channel",),
    "permission_prompt": ("permission_prompt",),
    "crash_timeout_cleanup": ("crash_timeout_cleanup",),
    "tool_call_result": ("tool_call_result_projection",),
    "raw_evidence_capture": ("collect_raw_evidence",),
    "parse_normalize": ("parse_ingest_project",),
    "db_ingest": ("db_ingest_project",),
    "session_projection": ("session_projection",),
    "timeline_projection": ("timeline_projection",),
    "baseline_compare": ("baseline_compare",),
    "old_new_release_diff": ("old_new_release_diff",),
}


@dataclass(frozen=True)
class ActionDefinition:
    action_id: str
    title: str
    category: str
    contract_operation: str | None
    support_kind: str
    required_evidence: str
    description: str


ACTION_DEFINITIONS: tuple[ActionDefinition, ...] = (
    ActionDefinition(
        "provider_identity",
        "Provider Identity",
        "identity",
        None,
        "harness",
        "binary_version",
        "Resolve the provider adapter, binary identity, version command, and managed-provider contract.",
    ),
    ActionDefinition(
        "launch_local",
        "Launch Local Session",
        "control",
        "launch_local",
        "contract_bool",
        "live_no_token",
        "Start or attach a managed session on the same machine without spending model tokens.",
    ),
    ActionDefinition(
        "launch_remote",
        "Launch Remote Session",
        "control",
        "launch_remote",
        "contract_bool",
        "hermetic",
        "Launch or continue a managed session through the Runtime Host/Machine Agent control plane.",
    ),
    ActionDefinition(
        "run_once",
        "Run Prompt Once",
        "control",
        "run_once",
        "contract_bool",
        "hermetic",
        "Run a bounded one-shot provider prompt and bind it to Longhouse evidence.",
    ),
    ActionDefinition(
        "session_identity",
        "Session Identity",
        "control",
        "reattach",
        "session_identity",
        "hermetic",
        "Preserve and verify provider session identity across Longhouse projections.",
    ),
    ActionDefinition(
        "send_message",
        "Send Message",
        "control",
        "send_input",
        "contract_bool",
        "hermetic",
        "Deliver a user message into a managed provider session.",
    ),
    ActionDefinition(
        "steer_active_turn",
        "Steer Active Turn",
        "control",
        "steer_active_turn",
        "contract_bool",
        "live_token",
        "Inject mid-turn steering text while a provider turn is active.",
    ),
    ActionDefinition(
        "pause_request_detect",
        "Detect Pause Request",
        "observe",
        "runtime_phase",
        "pause_request",
        "hermetic",
        "Detect provider/user-question pause states and project them as answerable pause requests.",
    ),
    ActionDefinition(
        "answer_pause_request",
        "Answer Pause Request",
        "control",
        None,
        "machine_capability:answer_pause",
        "hermetic",
        "Send an answer/reject/cancel decision for a pending provider question.",
    ),
    ActionDefinition(
        "interrupt_cancel",
        "Interrupt Or Cancel",
        "control",
        "interrupt",
        "contract_bool",
        "hermetic",
        "Interrupt an active provider turn or cancel a queued Longhouse input.",
    ),
    ActionDefinition(
        "resume_reattach",
        "Resume Or Reattach",
        "control",
        "reattach",
        "contract_bool",
        "live_no_token",
        "Reconnect to a prior provider session and verify the same transcript/session identity.",
    ),
    ActionDefinition(
        "terminate_cleanup",
        "Terminate Cleanup",
        "control",
        "terminate",
        "contract_bool",
        "hermetic",
        "Stop the managed provider process/control path and clean up owned resources.",
    ),
    ActionDefinition(
        "tail_output",
        "Tail Output",
        "observe",
        "tail_output",
        "contract_bool",
        "hermetic",
        "Observe fresh provider output or transcript tails without taking ownership away from the provider.",
    ),
    ActionDefinition(
        "runtime_phase",
        "Runtime Phase",
        "observe",
        "runtime_phase",
        "contract_bool",
        "hermetic",
        "Project provider runtime phase signals such as running, idle, needs_user, or blocked.",
    ),
    ActionDefinition(
        "transcript_binding",
        "Transcript Binding",
        "observe",
        "transcript_binding",
        "contract_bool",
        "hermetic",
        "Bind raw provider output to canonical Longhouse events and the session it came from.",
    ),
    ActionDefinition(
        "multi_turn_continuity",
        "Multi-turn Continuity",
        "control",
        "send_input",
        "contract_bool",
        "hermetic",
        "Preserve provider session identity and context across follow-up user turns.",
    ),
    ActionDefinition(
        "external_event_channel",
        "External Event Channel",
        "observe",
        None,
        "external_event_channel",
        "hermetic",
        "Observe provider hook/inbox/external-event delivery where the provider exposes it.",
    ),
    ActionDefinition(
        "permission_prompt",
        "Permission Prompt",
        "control",
        None,
        "permission_prompt",
        "live_token_required",
        "Observe and answer provider permission approve/deny prompts where supported.",
    ),
    ActionDefinition(
        "crash_timeout_cleanup",
        "Crash Timeout Cleanup",
        "resilience",
        None,
        "harness",
        "hermetic",
        "Timeout or crash leaves diagnostic artifacts and no owned managed process behind.",
    ),
    ActionDefinition(
        "tool_call_result",
        "Tool Call Result",
        "observe",
        "transcript_binding",
        "tool_result",
        "hermetic",
        "Parse provider tool calls/results without losing ids, names, inputs, content, or error state.",
    ),
    ActionDefinition(
        "raw_evidence_capture",
        "Raw Evidence Capture",
        "evidence",
        None,
        "harness",
        "hermetic",
        "Persist raw command output, provider events, logs, and canary artifacts before judging them.",
    ),
    ActionDefinition(
        "parse_normalize",
        "Parse And Normalize",
        "evidence",
        None,
        "harness",
        "hermetic",
        "Convert raw provider events into canonical Longhouse event rows while preserving unknowns.",
    ),
    ActionDefinition(
        "db_ingest",
        "Database Ingest",
        "projection",
        None,
        "longhouse_db",
        "hermetic",
        "Insert canonical events into the Longhouse DB and verify durable query/read surfaces.",
    ),
    ActionDefinition(
        "session_projection",
        "Session Projection",
        "projection",
        None,
        "harness",
        "hermetic",
        "Build the session-detail projection from canonical events and managed-control state.",
    ),
    ActionDefinition(
        "timeline_projection",
        "Timeline Projection",
        "projection",
        None,
        "harness",
        "hermetic",
        "Build the timeline/card projection from canonical events and managed-control state.",
    ),
    ActionDefinition(
        "baseline_compare",
        "Baseline Compare",
        "release_diff",
        None,
        "release_proof",
        "hermetic",
        "Compare current provider proof artifacts with stored expected behavior baselines.",
    ),
    ActionDefinition(
        "old_new_release_diff",
        "Old/New Release Diff",
        "release_diff",
        None,
        "release_proof",
        "live_no_token",
        "Run old and new provider releases through the same matrix and flag divergent behavior.",
    ),
)
ACTIONS = tuple(action.action_id for action in ACTION_DEFINITIONS)
CONTROL_SURFACE_CATEGORIES = frozenset({"control", "observe"})


def _action_ids_for_categories(categories: frozenset[str]) -> tuple[str, ...]:
    action_ids: list[str] = []
    for action in ACTION_DEFINITIONS:
        if action.category in categories:
            action_ids.append(action.action_id)
    return tuple(action_ids)


CONTROL_SURFACE_ACTION_IDS = _action_ids_for_categories(CONTROL_SURFACE_CATEGORIES)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"{path} contains a non-object JSONL row")
        rows.append(value)
    return rows


def command_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "argv": list(result.args) if isinstance(result.args, list) else result.args,
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
    }


@dataclass(frozen=True)
class AdapterConfig:
    provider: str
    binary_name: str
    binary_env: str | None
    capabilities: tuple[str, ...] = MVP_CAPABILITIES
    profiles: tuple[str, ...] = PROFILES
    methods: tuple[str, ...] = MVP_METHODS
    safe_run_prompt_once: bool = False
    safe_managed_session_scenarios: tuple[str, ...] = ()
    real_managed_session_e2e: bool = False


@dataclass(frozen=True)
class HarnessOptions:
    providers: tuple[str, ...]
    scenarios: tuple[str, ...]
    evidence_root: Path
    provider_bins: Mapping[str, Path] | None = None
    fixture_path: Path | None = None
    prompt: str | None = None
    old_proof_path: Path | None = None
    new_proof_path: Path | None = None
    old_proof_paths: Mapping[str, Path] | None = None
    new_proof_paths: Mapping[str, Path] | None = None
    baseline_root: Path | None = None


@dataclass(frozen=True)
class ScenarioResult:
    provider: str
    scenario: str
    status: str
    evidence_root: Path
    message: str | None = None
    failure_code: str | None = None
    data: Mapping[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "scenario": self.scenario,
            "status": self.status,
            "evidence_root": str(self.evidence_root),
        }
        if self.message:
            payload["message"] = self.message
        if self.failure_code:
            payload["failure_code"] = self.failure_code
        if self.data:
            payload["data"] = dict(self.data)
        return payload


class AgentHarnessAdapter(Protocol):
    config: AdapterConfig

    def prepare(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def probe(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def adapter_conformance(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def action_result(
        self,
        package: "EvidencePackage",
        action: ActionDefinition,
        *,
        probe: Mapping[str, Any],
        files: Iterable[str],
    ) -> dict[str, Any]: ...

    def action_matrix(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def control_surface(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def run_prompt(self, package: "EvidencePackage", prompt: str) -> dict[str, Any]: ...

    def collect_evidence(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def decode_normalize(self, package: "EvidencePackage", fixture_path: Path) -> dict[str, Any]: ...

    def db_ingest_project(self, package: "EvidencePackage", fixture_path: Path | None) -> dict[str, Any]: ...

    def session_projection(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def timeline_projection(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def launch_managed_session(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def send_receive(self, package: "EvidencePackage", prompt: str) -> dict[str, Any]: ...

    def steer_active_turn(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def pause_request_detect(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def answer_pause_request(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def interrupt_cancel(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def tool_call_result(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def resume_reattach(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def terminate_cleanup(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def tail_output(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def runtime_phase(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def transcript_binding(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def multi_turn_continuity(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def external_event_channel(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def permission_prompt(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def crash_timeout_cleanup(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def live_token_streaming(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def baseline_compare(
        self,
        package: "EvidencePackage",
        *,
        baseline_root: Path | None,
    ) -> dict[str, Any]: ...

    def old_new_release_diff(
        self,
        package: "EvidencePackage",
        *,
        old_proof_path: Path | None,
        new_proof_path: Path | None,
        baseline_root: Path | None,
    ) -> dict[str, Any]: ...

    def managed_session_e2e(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def cleanup(self, package: "EvidencePackage") -> dict[str, Any]: ...


class EvidencePackage:
    def __init__(self, *, root: Path, provider: str, scenario: str) -> None:
        self.root = root / provider / scenario
        self.provider = provider
        self.scenario = scenario

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def write_json(self, relative_path: str, payload: Mapping[str, Any]) -> Path:
        path = self.path(*relative_path.split("/"))
        write_json(path, payload)
        return path

    def write_text(self, relative_path: str, text: str) -> Path:
        path = self.path(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def initialize(self, *, adapter: AdapterConfig) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.write_json(
            "manifest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "artifact_kind": "universal_agent_harness_evidence",
                "provider": self.provider,
                "scenario": self.scenario,
                "adapter": adapter_snapshot(adapter),
                "generated_at": utc_now(),
            },
        )


class UniversalProviderAdapter:
    def __init__(self, config: AdapterConfig, *, provider_bin: Path | None = None) -> None:
        self.config = config
        self.provider_bin = provider_bin

    @property
    def adapter_name(self) -> str:
        return type(self).__name__

    def prepare(self, package: EvidencePackage) -> dict[str, Any]:
        package.initialize(adapter=self.config)
        workspace = package.path("workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": STATUS_PASS,
            "workspace": str(workspace),
            "provider": self.config.provider,
            "adapter_class": self.adapter_name,
            "methods": list(self.config.methods),
            "capabilities": list(self.config.capabilities),
        }
        package.write_json("assertions/prepare.json", payload)
        return payload

    def probe(self, package: EvidencePackage) -> dict[str, Any]:
        binary, source = self._resolve_binary()
        contract = contract_for_provider(self.config.provider)
        base = {
            "provider": self.config.provider,
            "binary_name": self.config.binary_name,
            "binary_env": self.config.binary_env,
            "binary_source": source,
            "declared_capabilities": list(self.config.capabilities),
            "declared_profiles": list(self.config.profiles),
            "mvp_methods": list(self.config.methods),
            "adapter_class": self.adapter_name,
            "managed_contract": {
                "managed_transport": str(contract.managed_transport) if contract else None,
                "control_plane": contract.control_plane if contract else None,
                "machine_control_supports": list(contract.machine_control_supports) if contract else [],
            },
        }
        if binary is None:
            payload = {
                **base,
                "status": STATUS_FAIL,
                "failure_code": "provider_binary_not_found",
                "message": f"{self.config.binary_name} binary was not found",
            }
            package.write_json("assertions/probe.json", payload)
            package.write_json(
                "raw/version-command.json",
                {"argv": [self.config.binary_name, "--version"], "error": payload["message"]},
            )
            return payload
        result = subprocess.run(
            [str(binary), "--version"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        evidence = command_evidence(result)
        package.write_json("raw/version-command.json", evidence)
        package.write_text("raw/stdout.log", result.stdout or "")
        package.write_text("raw/stderr.log", result.stderr or "")
        version = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0 or not version:
            payload = {
                **base,
                "status": STATUS_FAIL,
                "path": str(binary),
                "failure_code": "provider_version_failed",
                "message": f"{self.config.binary_name} --version failed",
                "command": evidence,
            }
        else:
            payload = {
                **base,
                "status": STATUS_PASS,
                "path": str(binary),
                "version": version,
                "command": evidence,
            }
        package.write_json("assertions/probe.json", payload)
        return payload

    def adapter_conformance(self, package: EvidencePackage) -> dict[str, Any]:
        declared_methods = set(self.config.methods)
        required_methods = set(MVP_METHODS)
        method_rows = []
        for method_name in MVP_METHODS:
            method = getattr(self, method_name, None)
            method_rows.append(
                {
                    "method": method_name,
                    "declared": method_name in declared_methods,
                    "callable": callable(method),
                }
            )
        expected_class = ADAPTER_CLASS_BY_PROVIDER.get(self.config.provider, UniversalProviderAdapter).__name__
        action_mapping_keys = set(ACTION_EXECUTION_SCENARIO_BY_ID)
        unmapped_actions = sorted(set(ACTIONS) - action_mapping_keys)
        extra_action_mappings = sorted(action_mapping_keys - set(ACTIONS))
        mapped_scenarios: set[str] = set()
        for scenarios in ACTION_EXECUTION_SCENARIO_BY_ID.values():
            mapped_scenarios.update(scenarios)
        mapped_unknown_scenarios = sorted(set(mapped_scenarios) - set(SCENARIOS))
        missing_scenario_runners = sorted(set(SCENARIOS) - set(SCENARIO_RUNNERS))
        extra_scenario_runners = sorted(set(SCENARIO_RUNNERS) - set(SCENARIOS))
        action_execution_scenarios: dict[str, list[str]] = {}
        for action_id, scenarios in ACTION_EXECUTION_SCENARIO_BY_ID.items():
            action_execution_scenarios[action_id] = list(scenarios)
        failures = {
            "missing_declared_methods": sorted(required_methods - declared_methods),
            "extra_declared_methods": sorted(declared_methods - required_methods),
            "missing_callable_methods": sorted(row["method"] for row in method_rows if not row["callable"]),
            "wrong_adapter_class": type(self).__name__ != expected_class,
            "unmapped_actions": unmapped_actions,
            "extra_action_mappings": extra_action_mappings,
            "mapped_unknown_scenarios": mapped_unknown_scenarios,
            "missing_scenario_runners": missing_scenario_runners,
            "extra_scenario_runners": extra_scenario_runners,
        }
        passed = not any(bool(value) for value in failures.values())
        payload = {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "adapter_conformance",
            "provider": self.config.provider,
            "adapter_class": type(self).__name__,
            "expected_adapter_class": expected_class,
            "method_count": len(MVP_METHODS),
            "methods": method_rows,
            "capabilities": list(self.config.capabilities),
            "profiles": list(self.config.profiles),
            "action_count": len(ACTIONS),
            "action_ids": list(ACTIONS),
            "scenario_count": len(SCENARIOS),
            "scenario_ids": list(SCENARIOS),
            "action_execution_scenarios": action_execution_scenarios,
            "failures": failures,
            "operation_evidence": {
                "adapter_conformance": {
                    "status": STATUS_PASS if passed else STATUS_FAIL,
                    "level": "hermetic",
                    "canary": "universal_adapter_conformance",
                    "failure_code": None if passed else "adapter_conformance_failed",
                }
            },
        }
        if not passed:
            payload["failure_code"] = "adapter_conformance_failed"
            payload["message"] = "Provider adapter no longer conforms to the universal harness contract."
        package.write_json("assertions/adapter-conformance.json", payload)
        package.write_json("assertions/adapter_conformance.json", payload)
        return payload

    def action_result(
        self,
        package: EvidencePackage,
        action: ActionDefinition,
        *,
        probe: Mapping[str, Any],
        files: Iterable[str],
    ) -> dict[str, Any]:
        contract = contract_for_provider(self.config.provider)
        support, support_reason = _action_support(self.config.provider, action, contract)
        contract_evidence: dict[str, Any] = {}
        if contract is not None and action.contract_operation:
            contract_evidence = dict(contract.operation_evidence_for(action.contract_operation))
        row = {
            "action_id": action.action_id,
            "title": action.title,
            "category": action.category,
            "provider": self.config.provider,
            "adapter_class": self.adapter_name,
            "adapter_method": "action_result",
            "implementation_kind": _action_implementation_kind(
                action=action,
                support=support,
                contract_evidence=contract_evidence,
            ),
            "support": support,
            "support_reason": support_reason,
            "required_evidence": action.required_evidence,
            "description": action.description,
            "contract_operation": action.contract_operation,
            "contract_evidence": contract_evidence,
        }
        row.update(
            _action_status(
                action=action,
                support=support,
                support_reason=support_reason,
                contract_evidence=contract_evidence,
                provider=self.config.provider,
                probe=probe,
                files=list(files),
                package=package,
            )
        )
        return row

    def run_prompt(self, package: EvidencePackage, prompt: str) -> dict[str, Any]:
        package.write_text("input/prompt.txt", prompt)
        if not self.config.safe_run_prompt_once:
            payload = self._unsupported_payload(
                "run_prompt_once",
                "run_prompt_once_not_safe_no_token",
                "run_prompt_once is not yet safe to claim without a token-spending provider run.",
            )
            payload["operation_evidence"] = {
                "run_once": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_run_prompt_once",
                    "failure_code": "run_prompt_once_not_safe_no_token",
                }
            }
        else:
            probe = self.probe(package)
            if probe.get("status") != STATUS_PASS:
                payload = {
                    **probe,
                    "status": STATUS_FAIL,
                    "failure_code": probe.get("failure_code") or "run_prompt_probe_failed",
                }
            else:
                payload = self._write_message_exchange(
                    package,
                    prompt=prompt,
                    scenario="run_prompt_once",
                    operation="run_once",
                    canary="universal_run_prompt_once",
                    level="hermetic",
                    source="universal harness provider-neutral prompt projection; does not prove live model output",
                )
        package.write_json("assertions/run_prompt.json", payload)
        return payload

    def collect_evidence(self, package: EvidencePackage) -> dict[str, Any]:
        files = sorted(str(path.relative_to(package.root)) for path in package.root.rglob("*") if path.is_file())
        payload = {
            "status": STATUS_PASS,
            "files": files,
            "required_dirs": ["raw", "events", "longhouse", "assertions"],
        }
        for dirname in payload["required_dirs"]:
            package.path(str(dirname)).mkdir(parents=True, exist_ok=True)
        package.write_json("assertions/collect_raw_evidence.json", payload)
        return payload

    def action_matrix(self, package: EvidencePackage) -> dict[str, Any]:
        probe = self.probe(package)
        files = sorted(str(path.relative_to(package.root)) for path in package.root.rglob("*") if path.is_file())
        rows = self._build_action_matrix_rows(package=package, probe=probe, files=files)
        action_matrix = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "universal_agent_harness_action_matrix",
            "provider": self.config.provider,
            "generated_at": utc_now(),
            "actions": rows,
            "action_ids": [row["action_id"] for row in rows],
            "status_counts": _status_counts(row["status"] for row in rows),
        }
        action_matrix_path = package.write_json("assertions/action-matrix.json", action_matrix)
        raw_path = package.write_json(
            "raw/action-matrix-inputs.json",
            {
                "provider": self.config.provider,
                "probe": probe,
                "files": files,
                "contract": _contract_snapshot(self.config.provider),
            },
        )
        operation_evidence: dict[str, dict[str, Any]] = {}
        for action, row in zip(ACTION_DEFINITIONS, rows, strict=True):
            if action.contract_operation and row.get("status") == STATUS_PASS:
                operation_evidence.setdefault(str(action.contract_operation), _operation_from_action_row(row))
        matrix_status = STATUS_PASS
        if any(row["status"] == STATUS_FAIL for row in rows):
            matrix_status = STATUS_FAIL
        elif any(row["status"] in YELLOW_STATUSES for row in rows):
            matrix_status = STATUS_BLOCKED
        payload = {
            "status": matrix_status,
            "action_count": len(rows),
            "action_ids": [row["action_id"] for row in rows],
            "action_matrix_path": str(action_matrix_path),
            "raw_inputs_path": str(raw_path),
            "status_counts": action_matrix["status_counts"],
            "actions": rows,
            "operation_evidence": operation_evidence,
        }
        package.write_json("assertions/action_matrix.json", payload)
        return payload

    def control_surface(self, package: EvidencePackage) -> dict[str, Any]:
        probe = self.probe(package)
        files = sorted(str(path.relative_to(package.root)) for path in package.root.rglob("*") if path.is_file())
        rows = [
            row
            for row in self._build_action_matrix_rows(package=package, probe=probe, files=files)
            if row["action_id"] in CONTROL_SURFACE_ACTION_IDS
        ]
        control_surface = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "universal_agent_harness_control_surface",
            "provider": self.config.provider,
            "generated_at": utc_now(),
            "actions": rows,
            "action_ids": [row["action_id"] for row in rows],
            "status_counts": _status_counts(row["status"] for row in rows),
        }
        control_surface_path = package.write_json("assertions/control-surface.json", control_surface)
        raw_path = package.write_json(
            "raw/control-surface-inputs.json",
            {
                "provider": self.config.provider,
                "probe": probe,
                "files": files,
                "contract": _contract_snapshot(self.config.provider),
                "control_surface_action_ids": list(CONTROL_SURFACE_ACTION_IDS),
            },
        )
        operation_evidence: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.get("status") == STATUS_PASS:
                operation_evidence[str(row["action_id"])] = _operation_from_action_row(row)
        status = STATUS_PASS
        if any(row["status"] == STATUS_FAIL for row in rows):
            status = STATUS_FAIL
        elif any(row["status"] in YELLOW_STATUSES for row in rows):
            status = STATUS_BLOCKED
        payload = {
            "status": status,
            "action_count": len(rows),
            "action_ids": [row["action_id"] for row in rows],
            "control_surface_path": str(control_surface_path),
            "raw_inputs_path": str(raw_path),
            "status_counts": control_surface["status_counts"],
            "actions": rows,
            "operation_evidence": operation_evidence,
        }
        package.write_json("assertions/control_surface.json", payload)
        return payload

    def decode_normalize(self, package: EvidencePackage, fixture_path: Path) -> dict[str, Any]:
        try:
            rows = read_json_lines(fixture_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "fixture_decode_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            package.write_json("assertions/parse_ingest_project.json", payload)
            return payload

        raw_path = package.write_text(
            "events/provider-raw-events.jsonl",
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        )
        canonical: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            canonical.append(canonical_event_from_fixture(row, provider=self.config.provider, index=index))
        canonical_path = package.write_text(
            "events/canonical-longhouse-events.jsonl",
            "\n".join(json.dumps(row, sort_keys=True) for row in canonical) + "\n",
        )
        unknown_path = package.write_text(
            "events/unknown-provider-events.jsonl",
            "\n".join(json.dumps(row, sort_keys=True) for row in rows if row.get("type") == "unknown") + "\n",
        )
        session_projection = project_session(canonical, provider=self.config.provider)
        timeline_projection = project_timeline(canonical)
        package.write_json("longhouse/session-projection.json", session_projection)
        package.write_json("longhouse/timeline-projection.json", timeline_projection)
        payload = {
            "status": STATUS_PASS,
            "fixture_path": str(fixture_path),
            "raw_event_count": len(rows),
            "canonical_event_count": len(canonical),
            "raw_events_path": str(raw_path),
            "canonical_events_path": str(canonical_path),
            "unknown_events_path": str(unknown_path),
            "session_projection_path": str(package.path("longhouse", "session-projection.json")),
            "timeline_projection_path": str(package.path("longhouse", "timeline-projection.json")),
        }
        package.write_json("assertions/parse_ingest_project.json", payload)
        return payload

    def db_ingest_project(self, package: EvidencePackage, fixture_path: Path | None) -> dict[str, Any]:
        try:
            rows = read_json_lines(fixture_path) if fixture_path is not None else default_db_ingest_rows()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "db_ingest_fixture_decode_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
            package.write_json("assertions/db_ingest_project.json", payload)
            return payload
        return ingest_canonical_events_into_longhouse_db(package=package, provider=self.config.provider, rows=rows)

    def session_projection(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._write_projection_surface(
            package,
            scenario="session_projection",
            operation="session_projection",
            canary="universal_session_projection",
        )
        package.write_json("assertions/session_projection.json", payload)
        return payload

    def timeline_projection(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._write_projection_surface(
            package,
            scenario="timeline_projection",
            operation="timeline_projection",
            canary="universal_timeline_projection",
        )
        package.write_json("assertions/timeline_projection.json", payload)
        return payload

    def launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
        if self.config.provider == "claude":
            return self._run_claude_launch_managed_session(package)
        if self.config.provider == "antigravity":
            return self._run_antigravity_launch_managed_session(package)
        if "launch_managed_session" not in self.config.safe_managed_session_scenarios:
            payload = self._unsupported_payload(
                "launch_managed_session",
                "managed_session_not_safe_no_token",
                "launch_managed_session is not yet backed by a no-token/session-safe universal adapter.",
            )
            payload["operation_evidence"] = {
                "launch_local": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_launch_managed_session",
                    "failure_code": "managed_session_not_safe_no_token",
                }
            }
            package.write_json("assertions/launch_managed_session.json", payload)
            return payload
        probe = self.probe(package)
        if probe.get("status") != STATUS_PASS:
            payload = {
                **probe,
                "status": STATUS_FAIL,
                "failure_code": probe.get("failure_code") or "launch_managed_session_probe_failed",
            }
            package.write_json("assertions/launch_managed_session.json", payload)
            return payload
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "session_start",
                    "role": "system",
                    "text": f"{self.config.provider} universal managed session launched",
                    "session_id": self._session_id(package),
                },
            ),
            operations={
                "launch_local": {
                    "status": STATUS_PASS,
                    "level": "live_no_token",
                    "canary": "universal_launch_managed_session",
                    "source": "universal harness binary identity plus managed-session projection",
                }
            },
        )
        package.write_json("assertions/launch_managed_session.json", payload)
        return payload

    def send_receive(self, package: EvidencePackage, prompt: str) -> dict[str, Any]:
        package.write_text("input/prompt.txt", prompt)
        if "send_receive" not in self.config.safe_managed_session_scenarios:
            payload = self._unsupported_payload(
                "send_receive",
                "send_receive_not_safe_no_token",
                "send_receive is not yet backed by a no-token/session-safe universal adapter.",
            )
            payload["operation_evidence"] = {
                "send_input": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_send_receive",
                    "failure_code": "send_receive_not_safe_no_token",
                },
                "transcript_binding": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_send_receive",
                    "failure_code": "send_receive_not_safe_no_token",
                },
            }
            package.write_json("assertions/send_receive.json", payload)
            return payload
        probe = self.probe(package)
        if probe.get("status") != STATUS_PASS:
            payload = {
                **probe,
                "status": STATUS_FAIL,
                "failure_code": probe.get("failure_code") or "send_receive_probe_failed",
            }
            package.write_json("assertions/send_receive.json", payload)
            return payload
        payload = self._write_message_exchange(
            package,
            prompt=prompt,
            scenario="send_receive",
            operation="send_input",
            canary="universal_send_receive",
            level="hermetic",
            source="universal harness provider-neutral send/receive projection; does not prove live model output",
        )
        package.write_json("assertions/send_receive.json", payload)
        return payload

    def steer_active_turn(self, package: EvidencePackage) -> dict[str, Any]:
        if self.config.provider == "claude":
            return self._run_claude_steer_active_turn(package)
        if self.config.provider == "codex":
            return self._run_codex_steer_active_turn(package)
        contract = contract_for_provider(self.config.provider)
        if contract is not None and contract.steer_active_turn:
            message = " ".join(
                [
                    "steer_active_turn is supported by the provider contract,",
                    "but is not yet backed by a universal provider adapter.",
                ]
            )
            payload = {
                "status": STATUS_BLOCKED,
                "scenario": "steer_active_turn",
                "failure_code": "steer_active_turn_adapter_missing",
                "message": message,
                "operation_evidence": {
                    "steer_active_turn": {
                        "status": STATUS_BLOCKED,
                        "level": "none",
                        "canary": "universal_steer_active_turn",
                        "failure_code": "steer_active_turn_adapter_missing",
                    }
                },
            }
        else:
            payload = self._unsupported_payload(
                "steer_active_turn",
                "steer_active_turn_unsupported",
                "This provider does not expose stable active-turn steering semantics.",
            )
            payload["operation_evidence"] = {
                "steer_active_turn": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_steer_active_turn",
                    "failure_code": "steer_active_turn_unsupported",
                }
            }
        package.write_json("assertions/steer_active_turn.json", payload)
        return payload

    def pause_request_detect(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._run_pause_request_service_projection(package, answer=False)
        package.write_json("assertions/pause_request_detect.json", payload)
        return payload

    def answer_pause_request(self, package: EvidencePackage) -> dict[str, Any]:
        if not _provider_answer_pause_supported(self.config.provider):
            payload = self._unsupported_payload(
                "answer_pause_request",
                "answer_pause_request_unsupported",
                "This provider does not expose stable answer-pause machine-control semantics.",
            )
            payload["operation_evidence"] = {
                "answer_pause_request": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_answer_pause_request",
                    "failure_code": "answer_pause_request_unsupported",
                }
            }
            package.write_json("assertions/answer_pause_request.json", payload)
            return payload

        payload = self._run_pause_request_service_projection(package, answer=True)
        package.write_json("assertions/answer_pause_request.json", payload)
        return payload

    def interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        if self.config.provider == "claude":
            return self._run_claude_interrupt_cancel(package)
        if self.config.provider == "codex":
            return self._run_codex_interrupt_cancel(package)
        if self.config.provider == "opencode":
            return self._run_opencode_interrupt_cancel(package)
        contract = contract_for_provider(self.config.provider)
        if contract is None or not contract.interrupt:
            payload = self._unsupported_payload(
                "interrupt_cancel",
                "interrupt_cancel_unsupported",
                "This provider does not expose stable interrupt/cancel semantics in the managed-provider contract.",
            )
            payload["operation_evidence"] = {
                "interrupt": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_interrupt_cancel",
                    "failure_code": "interrupt_cancel_unsupported",
                }
            }
            package.write_json("assertions/interrupt_cancel.json", payload)
            return payload
        payload = self._unsupported_payload(
            "interrupt_cancel",
            "interrupt_cancel_adapter_missing",
            "interrupt_cancel is not yet backed by a universal provider adapter for this provider.",
        )
        payload["operation_evidence"] = {
            "interrupt": {
                "status": STATUS_UNSUPPORTED_GAP,
                "level": "none",
                "canary": "universal_interrupt_cancel",
                "failure_code": "interrupt_cancel_adapter_missing",
            }
        }
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def tool_call_result(self, package: EvidencePackage) -> dict[str, Any]:
        if self.config.provider == "codex":
            return self._run_codex_tool_call_result(package)
        if self.config.provider == "opencode":
            return self._run_opencode_tool_call_result(package)
        payload = self._unsupported_payload(
            "tool_call_result",
            "tool_call_result_adapter_missing",
            "tool_call_result is not yet backed by a universal provider adapter for this provider.",
        )
        payload["operation_evidence"] = {
            "tool_call_result": {
                "status": STATUS_UNSUPPORTED_GAP,
                "level": "none",
                "canary": "universal_tool_call_result",
                "failure_code": "tool_call_result_adapter_missing",
            }
        }
        package.write_json("assertions/tool_call_result.json", payload)
        return payload

    def resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        if self.config.provider == "claude":
            return self._run_claude_resume_reattach(package)
        if self.config.provider == "opencode":
            return self._run_opencode_resume_reattach(package)
        if self.config.provider == "codex":
            return self._run_codex_resume_reattach(package)
        contract = contract_for_provider(self.config.provider)
        if contract is None or not (contract.reattach or contract.can_resume):
            payload = self._unsupported_payload(
                "resume_reattach",
                "resume_reattach_unsupported",
                "This provider does not expose stable resume/reattach semantics in the managed-provider contract.",
            )
            payload["operation_evidence"] = {
                "reattach": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_resume_reattach",
                    "failure_code": "resume_reattach_unsupported",
                }
            }
            package.write_json("assertions/resume_reattach.json", payload)
            return payload
        payload = self._unsupported_payload(
            "resume_reattach",
            "resume_reattach_adapter_missing",
            "resume_reattach is not yet backed by a universal provider adapter for this provider.",
        )
        payload["operation_evidence"] = {
            "reattach": {
                "status": STATUS_UNSUPPORTED_GAP,
                "level": "none",
                "canary": "universal_resume_reattach",
                "failure_code": "resume_reattach_adapter_missing",
            }
        }
        package.write_json("assertions/resume_reattach.json", payload)
        return payload

    def terminate_cleanup(self, package: EvidencePackage) -> dict[str, Any]:
        contract = contract_for_provider(self.config.provider)
        if contract is None or not contract.terminate:
            payload = self._unsupported_payload(
                "terminate_cleanup",
                "terminate_cleanup_unsupported",
                "This provider does not expose stable terminate/cleanup semantics in the managed-provider contract.",
            )
            payload["operation_evidence"] = {
                "terminate": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_terminate_cleanup",
                    "failure_code": "terminate_cleanup_unsupported",
                }
            }
            package.write_json("assertions/terminate_cleanup.json", payload)
            return payload
        payload = self._write_observation_projection(
            package,
            scenario="terminate_cleanup",
            operation="terminate",
            canary="universal_terminate_cleanup",
            raw_events=(
                {
                    "type": "session_start",
                    "role": "system",
                    "text": f"{self.config.provider} universal cleanup session started",
                    "provider_session_id": self._session_id(package),
                },
                {
                    "type": "terminal_signal",
                    "role": "system",
                    "text": f"{self.config.provider} universal cleanup released owned resources",
                    "provider_session_id": self._session_id(package),
                    "terminal_state": "session_ended",
                    "cleanup_owned_processes": 0,
                },
            ),
            source="universal harness provider-neutral cleanup projection; no live provider process was launched",
        )
        payload["cleanup_assertions"] = {
            "owned_process_launched": False,
            "owned_processes_remaining": 0,
            "terminal_event_projected": True,
        }
        package.write_json("assertions/terminate_cleanup.json", payload)
        return payload

    def tail_output(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._write_observation_projection(
            package,
            scenario="tail_output",
            operation="tail_output",
            canary="universal_tail_output",
            raw_events=(
                {
                    "type": "assistant",
                    "role": "assistant",
                    "text": f"{self.config.provider} tail output marker",
                    "provider_session_id": self._session_id(package),
                    "tail_offset": 1,
                },
                {
                    "type": "system",
                    "role": "system",
                    "text": f"{self.config.provider} tail output cursor advanced",
                    "provider_session_id": self._session_id(package),
                    "tail_cursor": "cursor-2",
                },
            ),
            source="universal harness canonical tail-output projection",
        )
        payload["tail_assertions"] = {
            "tail_event_count": 2,
            "cursor_observed": True,
            "assistant_tail_visible": True,
        }
        package.write_json("assertions/tail_output.json", payload)
        return payload

    def runtime_phase(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._run_runtime_phase_service_projection(package)
        package.write_json("assertions/runtime_phase.json", payload)
        return payload

    def transcript_binding(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._write_observation_projection(
            package,
            scenario="transcript_binding",
            operation="transcript_binding",
            canary="universal_transcript_binding",
            raw_events=(
                {
                    "type": "user",
                    "role": "user",
                    "text": f"{self.config.provider} transcript binding input",
                    "provider_session_id": self._session_id(package),
                    "source_path": "provider-transcript.jsonl",
                    "source_offset": 0,
                },
                {
                    "type": "assistant",
                    "role": "assistant",
                    "text": f"{self.config.provider} transcript binding output",
                    "provider_session_id": self._session_id(package),
                    "source_path": "provider-transcript.jsonl",
                    "source_offset": 1,
                },
            ),
            source="universal harness raw transcript to canonical event/session projection",
        )
        payload["binding_assertions"] = {
            "raw_transcript_projected": True,
            "provider_session_id_preserved": True,
            "user_and_assistant_bound": True,
        }
        package.write_json("assertions/transcript_binding.json", payload)
        return payload

    def multi_turn_continuity(self, package: EvidencePackage) -> dict[str, Any]:
        provider_session_id = self._session_id(package)
        payload = self._write_observation_projection(
            package,
            scenario="multi_turn_continuity",
            operation="multi_turn_continuity",
            canary="universal_multi_turn_continuity",
            raw_events=(
                {
                    "type": "user",
                    "role": "user",
                    "text": "Remember marker alpha.",
                    "provider_session_id": provider_session_id,
                    "turn_index": 1,
                },
                {
                    "type": "assistant",
                    "role": "assistant",
                    "text": "Marker alpha recorded.",
                    "provider_session_id": provider_session_id,
                    "turn_index": 1,
                },
                {
                    "type": "user",
                    "role": "user",
                    "text": "Use the marker from the prior turn.",
                    "provider_session_id": provider_session_id,
                    "turn_index": 2,
                    "depends_on_prior_turn": True,
                },
                {
                    "type": "assistant",
                    "role": "assistant",
                    "text": "Using marker alpha from the prior turn.",
                    "provider_session_id": provider_session_id,
                    "turn_index": 2,
                    "depends_on_prior_turn": True,
                },
            ),
            source="universal harness multi-turn canonical projection; does not prove live model memory",
        )
        operation_evidence = dict(payload.get("operation_evidence") or {})
        operation_evidence["send_input"] = {
            "status": STATUS_PASS,
            "level": "hermetic",
            "canary": "universal_multi_turn_continuity",
            "source": "universal harness multi-turn send projection",
        }
        payload["operation_evidence"] = operation_evidence
        payload["continuity_assertions"] = {
            "provider_session_id_stable": True,
            "turn_count": 2,
            "prior_turn_dependency_projected": True,
        }
        session_projection_path = package.path("longhouse", "session-projection.json")
        try:
            session_projection = json.loads(session_projection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            session_projection = {}
        if isinstance(session_projection, dict):
            session_projection["operation_statuses"] = operation_evidence
            package.write_json("longhouse/session-projection.json", session_projection)
        package.write_json("assertions/multi_turn_continuity.json", payload)
        return payload

    def external_event_channel(self, package: EvidencePackage) -> dict[str, Any]:
        if self.config.provider == "claude":
            return self._run_claude_provider_live_projection(
                package,
                scenario="external_event_channel",
                assertion_name="external_event_channel",
                require_operation="external_event_channel",
            )
        if self.config.provider != "antigravity":
            payload = self._unsupported_payload(
                "external_event_channel",
                "external_event_channel_unsupported",
                "This provider does not expose stable external-event channel semantics in the universal harness.",
            )
            payload["operation_evidence"] = {
                "external_event_channel": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_external_event_channel",
                    "failure_code": "external_event_channel_unsupported",
                }
            }
            package.write_json("assertions/external_event_channel.json", payload)
            return payload

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
        if self.config.provider == "opencode":
            return self._run_opencode_permission_prompt(package)
        if self.config.provider == "codex":
            return self._run_codex_permission_prompt(package)
        if self.config.provider == "antigravity":
            payload = self._unsupported_payload(
                "permission_prompt",
                "permission_prompt_unsupported",
                "Antigravity does not expose stable provider permission-prompt approve/deny semantics.",
            )
            payload["operation_evidence"] = {
                "permission_prompt": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_permission_prompt",
                    "failure_code": "permission_prompt_unsupported",
                }
            }
            package.write_json("assertions/permission_prompt.json", payload)
            return payload
        payload = {
            "status": STATUS_BLOCKED,
            "scenario": "permission_prompt",
            "failure_code": "permission_prompt_canary_missing",
            "message": "Permission prompt approve/deny behavior requires a provider-held prompt canary.",
            "operation_evidence": {
                "permission_prompt": {
                    "status": STATUS_BLOCKED,
                    "level": "live_token_required",
                    "canary": "universal_permission_prompt",
                    "failure_code": "permission_prompt_canary_missing",
                }
            },
            "next": "Add provider-specific permission prompt fixtures/canaries that prove approve and deny delivery.",
        }
        package.write_json("assertions/permission_prompt.json", payload)
        return payload

    def _run_codex_permission_prompt(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "permission_prompt")
        if binary_error is not None:
            return binary_error

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-permission-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "skip_static_contract": True,
                "run_fake_app_server": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)
        operation_evidence = self._operation_evidence_map(canary_artifact.get("operation_evidence"))
        permission = dict(operation_evidence.get("permission_prompt") or {})
        verdict = str(canary_artifact.get("verdict") or "red")
        passed = verdict == "green" and permission.get("status") == STATUS_PASS
        payload = {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "permission_prompt",
            "provider_version": canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": {
                "permission_prompt": permission
                or {
                    "status": STATUS_FAIL,
                    "level": "none",
                    "canary": "codex_fake_app_server_permission_approval",
                    "failure_code": "codex_permission_prompt_evidence_missing",
                }
            },
            "proof_scope": "codex_fake_app_server_permission_approval",
            "next": "Promote with a live held-permission Codex provider canary.",
        }
        if not passed:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_permission_prompt_failed"
            payload["message"] = "Codex fake app-server permission prompt canary did not pass."
        package.write_json("assertions/permission_prompt.json", payload)
        return payload

    def _run_opencode_permission_prompt(self, package: EvidencePackage) -> dict[str, Any]:
        from zerg.cli.opencode_bridge import permission_reply
        from zerg.services.opencode_bridge_state import write_opencode_bridge_state

        request_id = "perm-universal-opencode"
        decision = "allow"
        session_id = self._session_id(package)
        state_root = package.path("opencode-bridge-state")
        username = "opencode"
        password = "universal-permission-secret"
        requests: list[dict[str, Any]] = []
        expected_auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw_body = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw_body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    body = {"_decode_error": raw_body.decode("utf-8", errors="replace")}
                requests.append(
                    {
                        "path": self.path,
                        "authorization_ok": self.headers.get("Authorization") == expected_auth,
                        "body": body,
                    }
                )
                status = 204 if self.path == f"/permission/{request_id}/reply" else 404
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server_url = f"http://127.0.0.1:{server.server_address[1]}"
        state_path = write_opencode_bridge_state(
            session_id=session_id,
            server_url=server_url,
            server_username=username,
            server_password=password,
            cwd=str(package.root),
            opencode_pid=None,
            opencode_session_id="opencode-permission-session",
            state_root=state_root,
        )
        try:
            permission_reply(
                session_id=session_id,
                request_id=request_id,
                decision=decision,
                state_root=state_root,
                config_dir=None,
                wait_secs=0.0,
            )
            command_error = None
        except Exception as exc:
            command_error = f"{type(exc).__name__}: {exc}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        request = requests[0] if requests else {}
        assertions = {
            "request_received": bool(requests),
            "request_path_matches": request.get("path") == f"/permission/{request_id}/reply",
            "decision_forwarded": (request.get("body") or {}).get("decision") == decision,
            "auth_header_matches_state": request.get("authorization_ok") is True,
            "command_returned": command_error is None,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/opencode-permission-reply.json",
            {
                "server_url": server_url,
                "state_path": str(state_path),
                "session_id": session_id,
                "request_id": request_id,
                "decision": decision,
                "requests": requests,
                "command_error": command_error,
            },
        )
        operation_evidence = {
            "permission_prompt": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "opencode_bridge_permission_reply",
                "failure_code": None if passed else "opencode_permission_reply_failed",
            }
        }
        payload = {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "permission_prompt",
            "assertions": assertions,
            "raw_permission_reply_path": str(raw_path),
            "operation_evidence": operation_evidence,
            "proof_scope": "opencode_bridge_permission_reply",
        }
        if not passed:
            payload["failure_code"] = "opencode_permission_reply_failed"
            payload["message"] = "OpenCode bridge permission-reply transport did not pass."
        package.write_json("assertions/permission_prompt.json", payload)
        return payload

    def crash_timeout_cleanup(self, package: EvidencePackage) -> dict[str, Any]:
        package.write_text(
            "raw/timeout-diagnostics.log",
            "\n".join(
                [
                    "universal crash/timeout cleanup simulation",
                    "owned_process_launched=false",
                    "owned_processes_remaining=0",
                ]
            )
            + "\n",
        )
        payload = self._write_observation_projection(
            package,
            scenario="crash_timeout_cleanup",
            operation="crash_timeout_cleanup",
            canary="universal_crash_timeout_cleanup",
            raw_events=(
                {
                    "type": "runtime_phase",
                    "role": "system",
                    "text": f"{self.config.provider} timeout diagnostics captured",
                    "provider_session_id": self._session_id(package),
                    "phase": "blocked",
                    "failure_code": "simulated_timeout",
                },
                {
                    "type": "terminal_signal",
                    "role": "system",
                    "text": f"{self.config.provider} timeout cleanup released owned resources",
                    "provider_session_id": self._session_id(package),
                    "terminal_state": "timeout_cleanup_complete",
                    "owned_processes_remaining": 0,
                },
            ),
            source="universal harness crash/timeout diagnostic projection; no live provider process was launched",
        )
        payload["cleanup_assertions"] = {
            "diagnostics_written": True,
            "owned_process_launched": False,
            "owned_processes_remaining": 0,
            "terminal_cleanup_projected": True,
        }
        payload["diagnostics_path"] = str(package.path("raw", "timeout-diagnostics.log"))
        package.write_json("assertions/crash_timeout_cleanup.json", payload)
        return payload

    def live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        if self.config.provider == "claude":
            return self._run_claude_live_token_streaming(package)
        if self.config.provider == "codex":
            return self._run_codex_live_token_streaming(package)
        if self.config.provider == "opencode":
            return self._run_opencode_live_token_streaming(package)
        if self.config.provider == "antigravity":
            return self._run_antigravity_live_token_streaming(package)
        payload = self._unsupported_payload(
            "live_token_streaming",
            "live_token_streaming_adapter_missing",
            "live_token_streaming is not yet backed by a universal provider adapter for this provider.",
        )
        payload["operation_evidence"] = {
            "send_input": {
                "status": STATUS_UNSUPPORTED_GAP,
                "level": "none",
                "canary": "universal_live_token_streaming",
                "failure_code": "live_token_streaming_adapter_missing",
            },
            "live_token_behavior": {
                "status": STATUS_UNSUPPORTED_GAP,
                "level": "none",
                "canary": "universal_live_token_streaming",
                "failure_code": "live_token_streaming_adapter_missing",
            },
        }
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload

    def baseline_compare(
        self,
        package: EvidencePackage,
        *,
        baseline_root: Path | None,
    ) -> dict[str, Any]:
        action_matrix = self.action_matrix(package)
        control_surface = self.control_surface(package)
        session_projection = self.session_projection(package)
        base_proof_path = self._write_synthetic_release_proof(
            package,
            name="baseline",
            provider_version=f"{self.config.provider} universal-baseline",
            action_matrix=action_matrix,
            control_surface=control_surface,
            session_projection=session_projection,
        )
        candidate_proof_path = self._write_synthetic_release_proof(
            package,
            name="candidate",
            provider_version=f"{self.config.provider} universal-candidate",
            action_matrix=action_matrix,
            control_surface=control_surface,
            session_projection=session_projection,
        )
        baseline_script = default_repo_root() / "scripts" / "qa" / "provider-release-proof-baseline.py"
        diff_artifact_path = package.path("assertions", "baseline-compare-diff.json").resolve()
        root = (baseline_root or package.path("baseline-store")).resolve()
        stdout_path = package.path("raw", "baseline-compare-stdout.log")
        stderr_path = package.path("raw", "baseline-compare-stderr.log")
        command_path = package.path("raw", "baseline-compare-command.json")
        argv = [
            sys.executable,
            str(baseline_script),
            "diff",
            "--candidate",
            str(candidate_proof_path),
            "--base",
            str(base_proof_path),
            "--baseline-root",
            str(root),
            "--artifact",
            str(diff_artifact_path),
            "--json",
        ]
        if not baseline_script.is_file():
            payload = {
                "status": STATUS_BLOCKED,
                "scenario": "baseline_compare",
                "failure_code": "baseline_compare_tool_missing",
                "message": f"Baseline diff tool was not found at {baseline_script}.",
                "operation_evidence": {
                    "baseline_compare": {
                        "status": STATUS_BLOCKED,
                        "level": "artifact_diff",
                        "canary": "provider_release_proof_baseline_diff",
                        "failure_code": "baseline_compare_tool_missing",
                    }
                },
            }
            package.write_json("assertions/baseline_compare.json", payload)
            return payload
        try:
            result = subprocess.run(
                argv,
                cwd=str(default_repo_root()),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
            write_json(
                command_path,
                {
                    "argv": argv,
                    "returncode": None,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                },
            )
            payload = {
                "status": STATUS_FAIL,
                "scenario": "baseline_compare",
                "failure_code": "baseline_compare_exec_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "baseline_proof_path": str(base_proof_path),
                "candidate_proof_path": str(candidate_proof_path),
                "raw_command_path": str(command_path),
                "operation_evidence": {
                    "baseline_compare": {
                        "status": STATUS_FAIL,
                        "level": "artifact_diff",
                        "canary": "provider_release_proof_baseline_diff",
                        "failure_code": "baseline_compare_exec_failed",
                    }
                },
            }
            package.write_json("assertions/baseline_compare.json", payload)
            return payload

        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(result.stdout or "", encoding="utf-8")
        stderr_path.write_text(result.stderr or "", encoding="utf-8")
        write_json(command_path, command_evidence(result))
        try:
            diff_payload = _read_json(diff_artifact_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            payload = {
                "status": STATUS_FAIL,
                "scenario": "baseline_compare",
                "failure_code": "baseline_compare_artifact_unreadable",
                "message": f"{type(exc).__name__}: {exc}",
                "baseline_proof_path": str(base_proof_path),
                "candidate_proof_path": str(candidate_proof_path),
                "raw_command_path": str(command_path),
                "operation_evidence": {
                    "baseline_compare": {
                        "status": STATUS_FAIL,
                        "level": "artifact_diff",
                        "canary": "provider_release_proof_baseline_diff",
                        "failure_code": "baseline_compare_artifact_unreadable",
                    }
                },
            }
            package.write_json("assertions/baseline_compare.json", payload)
            return payload

        verdict = str(diff_payload.get("verdict") or "red")
        diff_status = str(dict(diff_payload.get("diff") or {}).get("status") or "unknown")
        passed = result.returncode == 0 and verdict == "green" and diff_status == "match"
        failure_code = None if passed else str(diff_payload.get("failure_code") or "baseline_compare_failed")
        payload = {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "baseline_compare",
            "provider_release_proof_diff_verdict": verdict,
            "baseline_proof_path": str(base_proof_path),
            "candidate_proof_path": str(candidate_proof_path),
            "baseline_compare_artifact_path": str(diff_artifact_path),
            "raw_command_path": str(command_path),
            "diff": diff_payload.get("diff"),
            "operation_evidence": {
                "baseline_compare": {
                    "status": STATUS_PASS if passed else STATUS_FAIL,
                    "level": "artifact_diff",
                    "canary": "provider_release_proof_baseline_diff",
                    "failure_code": failure_code,
                }
            },
        }
        if not passed:
            payload["failure_code"] = failure_code
            payload["message"] = "Provider release-proof baseline comparison did not match."
        package.write_json("assertions/baseline_compare.json", payload)
        return payload

    def _write_synthetic_release_proof(
        self,
        package: EvidencePackage,
        *,
        name: str,
        provider_version: str,
        action_matrix: Mapping[str, Any],
        control_surface: Mapping[str, Any],
        session_projection: Mapping[str, Any],
    ) -> Path:
        artifact_dir = package.path("baseline-compare", name, "evidence")
        normalized_contract = artifact_dir / "normalized" / "contract.json"
        provider_contract = artifact_dir / "normalized" / "provider_contract.json"
        operation_evidence_artifact = artifact_dir / "normalized" / "operation_evidence.json"
        session_projection_artifact = artifact_dir / "normalized" / "session_projection.json"
        action_matrix_artifact = artifact_dir / "normalized" / "action_matrix.json"
        control_surface_artifact = artifact_dir / "normalized" / "control_surface.json"
        source_artifact = artifact_dir / "source.json"
        stdout = artifact_dir / "stdout.log"
        stderr = artifact_dir / "stderr.log"
        operation_evidence = {
            "baseline_compare": {
                "status": STATUS_PASS,
                "level": "artifact_diff",
                "canary": "provider_release_proof_baseline_diff",
            }
        }
        normalized = {
            "artifact_kind": "provider_release_proof",
            "provider": self.config.provider,
            "provider_version": provider_version,
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "provider_release_proof_baseline_diff": {"status": "pass"},
            },
            "operation_evidence": operation_evidence,
        }
        provider_contract_payload = {
            "artifact_kind": "provider_release_proof_provider_contract",
            "provider": self.config.provider,
            "provider_version": provider_version,
            "contract_operations": {
                "baseline_compare": {
                    "level": "artifact_diff",
                    "source": "universal_agent_harness baseline_compare synthetic proof",
                }
            },
        }
        operation_evidence_payload = {
            "artifact_kind": "provider_release_proof_operation_evidence",
            "provider": self.config.provider,
            "provider_version": provider_version,
            "operation_evidence": operation_evidence,
        }
        session_projection_payload = {
            "artifact_kind": "provider_release_proof_session_projection",
            "provider": self.config.provider,
            "provider_version": provider_version,
            "status": "captured",
            "projection": {
                "artifact_kind": "provider_live_session_projection",
                "provider": self.config.provider,
                "status": "captured",
                "operation_statuses": operation_evidence,
                "checks": {
                    "baseline_compare": {"status": STATUS_PASS},
                },
            },
        }
        if isinstance(session_projection.get("operation_evidence"), Mapping):
            session_projection_payload["projection"]["operation_statuses"] = {
                **dict(session_projection.get("operation_evidence") or {}),
                **operation_evidence,
            }
        action_matrix_payload = {
            "artifact_kind": "provider_release_proof_action_matrix",
            "provider": self.config.provider,
            "provider_version": provider_version,
            "status": "captured",
            "action_matrix": {
                "artifact_kind": "provider_release_proof_action_matrix",
                "provider": self.config.provider,
                "action_count": action_matrix.get("action_count"),
                "action_ids": action_matrix.get("action_ids"),
                "status_counts": action_matrix.get("status_counts"),
                "actions": action_matrix.get("actions"),
            },
        }
        control_surface_payload = {
            "artifact_kind": "provider_release_proof_control_surface",
            "provider": self.config.provider,
            "provider_version": provider_version,
            "status": "captured",
            "control_surface": {
                "artifact_kind": "provider_release_proof_control_surface",
                "provider": self.config.provider,
                "action_count": control_surface.get("action_count"),
                "action_ids": control_surface.get("action_ids"),
                "status_counts": control_surface.get("status_counts"),
                "actions": control_surface.get("actions"),
            },
        }
        write_json(source_artifact, {"synthetic": True, "scenario": "baseline_compare"})
        source_artifact.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text("universal baseline compare\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        write_json(normalized_contract, normalized)
        write_json(provider_contract, provider_contract_payload)
        write_json(operation_evidence_artifact, operation_evidence_payload)
        write_json(session_projection_artifact, session_projection_payload)
        write_json(action_matrix_artifact, action_matrix_payload)
        write_json(control_surface_artifact, control_surface_payload)
        proof = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "provider_release_proof",
            "provider": self.config.provider,
            "provider_version": provider_version,
            "scenario_id": f"{self.config.provider}-universal-baseline-compare-v1",
            "scenario_version": 1,
            "verdict": "green",
            "failure_code": None,
            "normalized": normalized,
            "artifacts": {
                "source_artifact": str(source_artifact.resolve()),
                "stdout": str(stdout.resolve()),
                "stderr": str(stderr.resolve()),
                "normalized_contract": str(normalized_contract.resolve()),
                "provider_contract": str(provider_contract.resolve()),
                "operation_evidence": str(operation_evidence_artifact.resolve()),
                "session_projection": str(session_projection_artifact.resolve()),
                "action_matrix": str(action_matrix_artifact.resolve()),
                "control_surface": str(control_surface_artifact.resolve()),
            },
        }
        proof_path = package.path("baseline-compare", name, "proof.json")
        write_json(proof_path, proof)
        return proof_path.resolve()

    def old_new_release_diff(
        self,
        package: EvidencePackage,
        *,
        old_proof_path: Path | None,
        new_proof_path: Path | None,
        baseline_root: Path | None,
    ) -> dict[str, Any]:
        if old_proof_path is None or new_proof_path is None:
            next_step = " ".join(
                [
                    "Generate old/new provider release-proof artifacts, then rerun with",
                    "--old-proof-artifact and --new-proof-artifact.",
                ]
            )
            payload = {
                "status": STATUS_BLOCKED,
                "scenario": "old_new_release_diff",
                "failure_code": "old_new_proof_artifacts_required",
                "message": ("old_new_release_diff requires explicit old and new provider release-proof artifacts."),
                "operation_evidence": {
                    "old_new_release_diff": {
                        "status": STATUS_BLOCKED,
                        "level": "artifact_diff",
                        "canary": "provider_release_proof_old_new_diff",
                        "failure_code": "old_new_proof_artifacts_required",
                    }
                },
                "next": next_step,
            }
            package.write_json("assertions/old_new_release_diff.json", payload)
            return payload
        return self._run_old_new_release_diff(
            package,
            old_proof_path=old_proof_path,
            new_proof_path=new_proof_path,
            baseline_root=baseline_root,
        )

    def _run_old_new_release_diff(
        self,
        package: EvidencePackage,
        *,
        old_proof_path: Path,
        new_proof_path: Path,
        baseline_root: Path | None,
    ) -> dict[str, Any]:
        baseline_script = default_repo_root() / "scripts" / "qa" / "provider-release-proof-baseline.py"
        artifact_path = package.path("assertions", "old-new-release-diff.json").resolve()
        root = (baseline_root or package.path("baseline-store")).resolve()
        stdout_path = package.path("raw", "old-new-release-diff-stdout.log")
        stderr_path = package.path("raw", "old-new-release-diff-stderr.log")
        command_path = package.path("raw", "old-new-release-diff-command.json")
        argv = [
            sys.executable,
            str(baseline_script),
            "old-new",
            "--old",
            str(old_proof_path),
            "--new",
            str(new_proof_path),
            "--baseline-root",
            str(root),
            "--artifact",
            str(artifact_path),
            "--json",
        ]

        if not baseline_script.is_file():
            payload = {
                "status": STATUS_BLOCKED,
                "scenario": "old_new_release_diff",
                "failure_code": "old_new_diff_tool_missing",
                "message": f"Baseline diff tool was not found at {baseline_script}.",
                "old_proof_uri": str(old_proof_path),
                "new_proof_uri": str(new_proof_path),
                "operation_evidence": {
                    "old_new_release_diff": {
                        "status": STATUS_BLOCKED,
                        "level": "artifact_diff",
                        "canary": "provider_release_proof_old_new_diff",
                        "failure_code": "old_new_diff_tool_missing",
                    }
                },
            }
            package.write_json("assertions/old_new_release_diff.json", payload)
            return payload

        try:
            result = subprocess.run(
                argv,
                cwd=str(default_repo_root()),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
            command_payload = {
                "argv": argv,
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
            }
            write_json(command_path, command_payload)
            payload = {
                "status": STATUS_FAIL,
                "scenario": "old_new_release_diff",
                "failure_code": "old_new_diff_exec_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "old_proof_uri": str(old_proof_path),
                "new_proof_uri": str(new_proof_path),
                "raw_command_path": str(command_path),
                "operation_evidence": {
                    "old_new_release_diff": {
                        "status": STATUS_FAIL,
                        "level": "artifact_diff",
                        "canary": "provider_release_proof_old_new_diff",
                        "failure_code": "old_new_diff_exec_failed",
                    }
                },
            }
            package.write_json("assertions/old_new_release_diff.json", payload)
            return payload

        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(result.stdout or "", encoding="utf-8")
        stderr_path.write_text(result.stderr or "", encoding="utf-8")
        write_json(command_path, command_evidence(result))

        try:
            diff_payload = _read_json(artifact_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            diff_payload = {
                "artifact_kind": "provider_release_proof_old_new_diff",
                "verdict": "red",
                "failure_code": "old_new_diff_artifact_missing",
                "message": f"{type(exc).__name__}: {exc}",
                "old_proof_uri": str(old_proof_path),
                "new_proof_uri": str(new_proof_path),
            }

        verdict = str(diff_payload.get("verdict") or "red")
        status = STATUS_PASS if result.returncode == 0 and verdict == "green" else STATUS_FAIL
        failure_code = None
        if status != STATUS_PASS:
            failure_code = str(diff_payload.get("failure_code") or "old_new_release_diff_failed")
        payload = {
            "status": status,
            "scenario": "old_new_release_diff",
            "old_proof_uri": str(diff_payload.get("old_proof_uri") or old_proof_path),
            "new_proof_uri": str(diff_payload.get("new_proof_uri") or new_proof_path),
            "old_new_diff_artifact_path": str(artifact_path),
            "raw_command_path": str(command_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "provider_release_proof_old_new_verdict": verdict,
            "diff": diff_payload.get("diff"),
            "staging": diff_payload.get("staging"),
            "operation_evidence": {
                "old_new_release_diff": {
                    "status": status,
                    "level": "artifact_diff",
                    "canary": "provider_release_proof_old_new_diff",
                    "failure_code": failure_code,
                }
            },
        }
        if failure_code:
            payload["failure_code"] = failure_code
            payload["message"] = "Old/new provider release-proof artifacts diverged."
        package.write_json("assertions/old_new_release_diff.json", payload)
        return payload

    def _run_pause_request_service_projection(self, package: EvidencePackage, *, answer: bool) -> dict[str, Any]:
        os.environ.setdefault("TESTING", "1")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

        from zerg.database import initialize_database
        from zerg.database import make_engine
        from zerg.database import make_sessionmaker
        from zerg.models.agents import AgentSession
        from zerg.models.agents import SessionPauseRequest
        from zerg.models.agents import SessionRuntimeState
        from zerg.services.session_pause_requests import PAUSE_KIND_STRUCTURED_QUESTION
        from zerg.services.session_pause_requests import list_pause_requests_for_session
        from zerg.services.session_pause_requests import load_active_pause_request_for_session
        from zerg.services.session_pause_requests import resolve_pause_request
        from zerg.services.session_pause_requests import serialize_pause_request_projection
        from zerg.services.session_runtime import RuntimeEventIngest
        from zerg.services.session_runtime import ingest_runtime_events
        from zerg.session_execution_home import ManagedSessionTransport
        from zerg.session_execution_home import SessionExecutionHome

        scenario = "answer_pause_request" if answer else "pause_request_detect"
        can_respond = _provider_answer_pause_supported(self.config.provider)
        managed_transport = {
            "claude": ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value,
            "codex": ManagedSessionTransport.CODEX_APP_SERVER.value,
        }.get(self.config.provider)
        now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        db_path = package.path("longhouse", "pause-request-service.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{db_path}")
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)

        with session_factory() as db:
            session = AgentSession(
                provider=self.config.provider,
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=now - timedelta(minutes=5),
                last_activity_at=now,
                user_messages=1,
                assistant_messages=0,
                execution_home=SessionExecutionHome.MANAGED_LOCAL.value if managed_transport else None,
                managed_transport=managed_transport,
                provider_session_id=f"{self.config.provider}-pause-answer-session" if managed_transport else None,
                managed_session_name=f"{self.config.provider}-pause-answer-proof" if managed_transport else None,
            )
            db.add(session)
            db.flush()
            db.refresh(session)
            runtime_key = f"{self.config.provider}:{session.id}:pause-request"
            question_payload = {
                "questions": [
                    {
                        "id": "approach",
                        "header": "Approach",
                        "question": "Which implementation approach should the agent use?",
                        "multiSelect": False,
                        "options": [
                            {
                                "label": "Small adapter path",
                                "description": "Keep this provider-specific change narrow.",
                            },
                            {"label": "Broad refactor", "description": "Reshape the whole provider bridge."},
                        ],
                    }
                ]
            }
            phase_result = ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key,
                        session_id=session.id,
                        provider=self.config.provider,
                        device_id="universal-harness",
                        source="universal_harness",
                        kind="phase_signal",
                        phase="needs_user",
                        occurred_at=now,
                        freshness_ms=10 * 60 * 1000,
                        dedupe_key=f"{scenario}:phase-needs-user",
                        payload={},
                    )
                ],
            )
            pause_result = ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key,
                        session_id=session.id,
                        provider=self.config.provider,
                        device_id="universal-harness",
                        source="universal_harness",
                        kind="pause_request",
                        tool_name=_provider_pause_tool_name(self.config.provider),
                        occurred_at=now + timedelta(seconds=1),
                        dedupe_key=f"{scenario}:pause-question",
                        payload={
                            "provider_request_id": "question-1",
                            "kind": PAUSE_KIND_STRUCTURED_QUESTION,
                            "title": "Choose approach",
                            "summary": "The provider needs a structured user decision before continuing.",
                            "request_payload": question_payload,
                            "can_respond": can_respond,
                            "provider_ref": {
                                "source": "universal_harness",
                                "scenario": scenario,
                            },
                        },
                    )
                ],
            )
            db.commit()

            state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one_or_none()
            active = load_active_pause_request_for_session(db, session.id)
            pending_projection = _json_safe(serialize_pause_request_projection(active, can_respond=can_respond))
            pending_rows = [
                _json_safe(serialize_pause_request_projection(row, can_respond=row.can_respond))
                for row in list_pause_requests_for_session(db, session.id)
            ]
            can_respond_matches_provider_contract = bool((pending_projection or {}).get("can_respond")) == can_respond
            pending_assertions = {
                "runtime_phase_needs_user": state is not None and state.phase == "needs_user",
                "active_pause_request_visible": active is not None,
                "pause_request_pending": active is not None and active.status == "pending",
                "question_payload_projected": bool((pending_projection or {}).get("questions")),
                "can_respond_matches_provider_contract": can_respond_matches_provider_contract,
            }

            resolved_projection = None
            active_after_response = None
            all_rows_after_response: list[dict[str, Any] | None] = []
            response_assertions: dict[str, bool] = {}
            answer_dispatch: dict[str, Any] | None = None
            if answer and active is not None:
                if can_respond:
                    from zerg.services import managed_local_control as control

                    calls: list[dict[str, Any]] = []

                    async def fake_dispatch(**kwargs: Any) -> SimpleNamespace:
                        calls.append(
                            {
                                "owner_id": kwargs.get("owner_id"),
                                "timeout_secs": kwargs.get("timeout_secs"),
                                "command_type": kwargs.get("command_type"),
                                "payload": kwargs.get("payload"),
                                "commis_id": kwargs.get("commis_id"),
                                "run_id": kwargs.get("run_id"),
                                "provider": getattr(kwargs.get("session"), "provider", None),
                                "managed_transport": getattr(kwargs.get("session"), "managed_transport", None),
                            }
                        )
                        return SimpleNamespace(
                            ok=True,
                            transport="engine_channel",
                            data={
                                "exit_code": 0,
                                "stdout": "",
                                "stderr": "",
                                "pause_response": {
                                    "request_key": active.request_key,
                                    "decision": "answer",
                                    "answers": {"approach": "Small adapter path"},
                                },
                            },
                            error=None,
                        )

                    original_dispatch = control.dispatch_managed_control_command
                    original_transport_error = control._managed_control_transport_error
                    control.dispatch_managed_control_command = fake_dispatch
                    control._managed_control_transport_error = lambda *_args, **_kwargs: None
                    try:
                        dispatch_result = asyncio.run(
                            control.answer_pause_request_on_managed_local_session(
                                db=db,
                                owner_id=1,
                                session=session,
                                request_key=active.request_key,
                                decision="answer",
                                answers={"approach": "Small adapter path"},
                                message="Use the small adapter path.",
                                commis_id=f"universal-{self.config.provider}-answer-pause",
                            )
                        )
                    finally:
                        control.dispatch_managed_control_command = original_dispatch
                        control._managed_control_transport_error = original_transport_error

                    request = calls[0] if calls else {}
                    expected_payload = {
                        "provider": self.config.provider,
                        "request_key": active.request_key,
                        "decision": "answer",
                        "answers": {"approach": "Small adapter path"},
                        "message": "Use the small adapter path.",
                    }
                    dispatch_assertions = {
                        "command_dispatched": bool(calls),
                        "command_type_matches": request.get("command_type") == "session.answer_pause",
                        "payload_matches": request.get("payload") == expected_payload,
                        "provider_matches": request.get("provider") == self.config.provider,
                        "transport_matches": request.get("managed_transport") == managed_transport,
                        "result_ok": dispatch_result.ok is True,
                        "exit_code_zero": dispatch_result.exit_code == 0,
                        "response_data_projected": bool(dispatch_result.response_data),
                    }
                    answer_dispatch = {
                        "calls": calls,
                        "result": {
                            "ok": dispatch_result.ok,
                            "exit_code": dispatch_result.exit_code,
                            "error": dispatch_result.error,
                            "response_data": dispatch_result.response_data,
                        },
                        "assertions": dispatch_assertions,
                    }
                resolved = resolve_pause_request(
                    db,
                    pause_request_id=active.id,
                    status="resolved",
                    occurred_at=now + timedelta(seconds=5),
                    response_payload={
                        "answers": {
                            "approach": "Small adapter path",
                        }
                    },
                    response_text="Use the small adapter path.",
                )
                db.commit()
                if resolved is not None:
                    db.refresh(resolved)
                active_after = load_active_pause_request_for_session(db, session.id)
                active_after_response = _json_safe(serialize_pause_request_projection(active_after))
                rows_after = (
                    db.query(SessionPauseRequest)
                    .filter(SessionPauseRequest.session_id == session.id)
                    .order_by(SessionPauseRequest.created_at.asc())
                    .all()
                )
                all_rows_after_response = []
                for row in rows_after:
                    projection = serialize_pause_request_projection(row, can_respond=row.can_respond)
                    all_rows_after_response.append(_json_safe(projection))
                resolved_projection = _json_safe(serialize_pause_request_projection(resolved, can_respond=can_respond))
                response_assertions = {
                    "pause_request_resolved": resolved is not None and resolved.status == "resolved",
                    "active_pause_request_cleared": active_after is None,
                    "response_payload_stored": bool(getattr(resolved, "response_payload_json", None)),
                    "response_text_stored": getattr(resolved, "response_text", None) == "Use the small adapter path.",
                }

            state_projection = None
            if state is not None:
                state_projection = {
                    "runtime_key": state.runtime_key,
                    "session_id": str(state.session_id) if state.session_id else None,
                    "provider": state.provider,
                    "phase": state.phase,
                    "phase_source": state.phase_source,
                    "active_tool": state.active_tool,
                    "phase_started_at": state.phase_started_at,
                    "last_runtime_signal_at": state.last_runtime_signal_at,
                    "runtime_version": state.runtime_version,
                }

            db_summary = {
                "session_id": str(session.id),
                "runtime_key": runtime_key,
                "db_path": str(db_path),
                "phase_result": phase_result.model_dump(mode="json"),
                "pause_result": pause_result.model_dump(mode="json"),
                "state": _json_safe(state_projection),
                "pending_pause_request": pending_projection,
                "pending_pause_requests": pending_rows,
                "resolved_pause_request": resolved_projection,
                "active_after_response": active_after_response,
                "all_rows_after_response": all_rows_after_response,
            }

        db_summary_path = package.write_json("longhouse/pause-request-service.json", _json_safe(db_summary))
        package.write_json(
            "longhouse/runtime-state.json",
            _json_safe({"state": db_summary["state"], "runtime_key": db_summary["runtime_key"]}),
        )
        package.write_json(
            "longhouse/pause-request-pending.json",
            _json_safe({"pause_request": pending_projection, "pause_requests": pending_rows}),
        )
        if answer:
            package.write_json(
                "longhouse/pause-request-resolved.json",
                _json_safe(
                    {
                        "resolved_pause_request": resolved_projection,
                        "active_after_response": active_after_response,
                        "all_rows_after_response": all_rows_after_response,
                    }
                ),
            )
            if answer_dispatch is not None:
                package.write_json("raw/answer-pause-dispatch.json", _json_safe(answer_dispatch))

        pending_pass = all(pending_assertions.values())
        service_pass = not answer or (pending_pass and all(response_assertions.values()))
        if not answer:
            status = STATUS_PASS if pending_pass else STATUS_FAIL
            failure_code = None if status == STATUS_PASS else "pause_request_detect_projection_failed"
            payload = {
                "status": status,
                "scenario": scenario,
                "db_path": str(db_path),
                "db_summary_path": str(db_summary_path),
                "runtime_key": db_summary["runtime_key"],
                "session_id": db_summary["session_id"],
                "runtime_state_path": str(package.path("longhouse", "runtime-state.json")),
                "pause_request_pending_path": str(package.path("longhouse", "pause-request-pending.json")),
                "pause_request": pending_projection,
                "assertions": pending_assertions,
                "operation_evidence": {
                    "pause_request_detect": {
                        "status": status,
                        "level": "hermetic",
                        "canary": "universal_pause_request_detect",
                        "failure_code": failure_code,
                    },
                    "runtime_phase": {
                        "status": STATUS_PASS if pending_assertions["runtime_phase_needs_user"] else STATUS_FAIL,
                        "level": "hermetic",
                        "canary": "universal_pause_request_detect",
                    },
                },
            }
            if failure_code:
                payload["failure_code"] = failure_code
                payload["message"] = "Longhouse did not project a pending structured pause request."
            return payload

        dispatch_assertions = dict((answer_dispatch or {}).get("assertions") or {})
        dispatch_pass = bool(dispatch_assertions) and all(dispatch_assertions.values())
        answer_failure_code = None
        if not service_pass:
            answer_failure_code = "answer_pause_request_service_failed"
        elif can_respond and not dispatch_pass:
            answer_failure_code = "answer_pause_dispatch_failed"
        answer_message = "Longhouse pause-request response service assertions failed."
        if service_pass and can_respond and not dispatch_pass:
            answer_message = "Longhouse managed-local pause answer dispatch assertions failed."
        next_step = " ".join(
            [
                "Promote with a live provider-held structured-question canary that proves the answer",
                "reaches the provider runtime.",
            ]
        )
        answer_status = STATUS_PASS if service_pass and (not can_respond or dispatch_pass) else STATUS_FAIL
        payload = {
            "status": answer_status,
            "scenario": scenario,
            "failure_code": answer_failure_code,
            "message": answer_message,
            "db_path": str(db_path),
            "db_summary_path": str(db_summary_path),
            "runtime_key": db_summary["runtime_key"],
            "session_id": db_summary["session_id"],
            "pause_request_pending_path": str(package.path("longhouse", "pause-request-pending.json")),
            "pause_request_resolved_path": str(package.path("longhouse", "pause-request-resolved.json")),
            "pause_request": pending_projection,
            "resolved_pause_request": resolved_projection,
            "active_after_response": active_after_response,
            "assertions": {**pending_assertions, **response_assertions},
            "longhouse_response_service": {
                "status": STATUS_PASS if service_pass else STATUS_FAIL,
                "level": "hermetic",
                "canary": "universal_answer_pause_request_service",
                "failure_code": None if service_pass else "answer_pause_request_service_failed",
            },
            "managed_answer_dispatch": {
                "status": STATUS_PASS if dispatch_pass else STATUS_FAIL,
                "level": "hermetic",
                "canary": "universal_answer_pause_dispatch",
                "failure_code": None if dispatch_pass else "answer_pause_dispatch_failed",
                "assertions": dispatch_assertions,
                "dispatch_path": str(package.path("raw", "answer-pause-dispatch.json")),
            }
            if can_respond
            else None,
            "operation_evidence": {
                "answer_pause_request": {
                    "status": answer_status,
                    "level": "hermetic" if answer_status == STATUS_PASS else "none",
                    "canary": "universal_answer_pause_dispatch" if can_respond else "universal_answer_pause_request",
                    "failure_code": answer_failure_code,
                },
                "live_answer_delivery": {
                    "status": STATUS_BLOCKED,
                    "level": "live_token_required",
                    "canary": "universal_answer_pause_provider_delivery",
                    "failure_code": "answer_pause_provider_delivery_unproven",
                },
                "pause_request_detect": {
                    "status": STATUS_PASS if pending_pass else STATUS_FAIL,
                    "level": "hermetic",
                    "canary": "universal_pause_request_detect",
                },
                "longhouse_pause_response_service": {
                    "status": STATUS_PASS if service_pass else STATUS_FAIL,
                    "level": "hermetic",
                    "canary": "universal_answer_pause_request_service",
                    "failure_code": None if service_pass else "answer_pause_request_service_failed",
                },
            },
            "next": next_step,
        }
        if answer_status == STATUS_PASS:
            payload.pop("failure_code", None)
            payload["message"] = (
                "Longhouse resolved the pause request and dispatched a managed-local pause answer; "
                "provider-held live answer delivery remains a stronger gate."
            )
        return payload

    def _write_observation_projection(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        operation: str,
        canary: str,
        raw_events: Iterable[Mapping[str, Any]],
        source: str,
    ) -> dict[str, Any]:
        operations = {
            operation: {
                "status": STATUS_PASS,
                "level": "hermetic",
                "canary": canary,
                "source": source,
            },
            "transcript_binding": {
                "status": STATUS_PASS,
                "level": "hermetic",
                "canary": canary,
                "source": "universal harness canonical event/session projection",
            },
        }
        payload = self._write_session_projection(
            package,
            raw_events=raw_events,
            operations=operations,
            provider_session_id=self._session_id(package),
        )
        payload["scenario"] = scenario
        return payload

    def _run_runtime_phase_service_projection(self, package: EvidencePackage) -> dict[str, Any]:
        os.environ.setdefault("TESTING", "1")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

        from zerg.database import initialize_database
        from zerg.database import make_engine
        from zerg.database import make_sessionmaker
        from zerg.models.agents import AgentSession
        from zerg.models.agents import SessionRuntimeState
        from zerg.services.session_runtime import RuntimeEventIngest
        from zerg.services.session_runtime import ingest_runtime_events

        now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        db_path = package.path("longhouse", "runtime-phase-service.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{db_path}")
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)

        with session_factory() as db:
            session = AgentSession(
                provider=self.config.provider,
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=now - timedelta(minutes=5),
                last_activity_at=now,
                user_messages=1,
                assistant_messages=1,
            )
            db.add(session)
            db.flush()
            db.refresh(session)
            runtime_key = f"{self.config.provider}:{session.id}:runtime-phase"
            events = [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider=self.config.provider,
                    device_id="universal-harness",
                    source="universal_harness",
                    kind="phase_signal",
                    phase="running",
                    tool_name="Shell",
                    occurred_at=now,
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="runtime-phase:running",
                    payload={},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider=self.config.provider,
                    device_id="universal-harness",
                    source="universal_harness",
                    kind="progress_signal",
                    phase="running",
                    tool_name="Shell",
                    occurred_at=now + timedelta(seconds=1),
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="runtime-phase:progress",
                    payload={"message": "runtime phase progress marker"},
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider=self.config.provider,
                    device_id="universal-harness",
                    source="universal_harness",
                    kind="phase_signal",
                    phase="idle",
                    occurred_at=now + timedelta(seconds=2),
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="runtime-phase:idle",
                    payload={},
                ),
            ]
            result = ingest_runtime_events(db, events)
            db.commit()
            state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one_or_none()
            state_projection = None
            if state is not None:
                state_projection = {
                    "runtime_key": state.runtime_key,
                    "session_id": str(state.session_id) if state.session_id else None,
                    "provider": state.provider,
                    "phase": state.phase,
                    "phase_source": state.phase_source,
                    "active_tool": state.active_tool,
                    "phase_started_at": state.phase_started_at,
                    "last_runtime_signal_at": state.last_runtime_signal_at,
                    "last_progress_at": state.last_progress_at,
                    "runtime_version": state.runtime_version,
                }
            db_summary = {
                "session_id": str(session.id),
                "runtime_key": runtime_key,
                "db_path": str(db_path),
                "ingest_result": result.model_dump(mode="json"),
                "state": _json_safe(state_projection),
            }

        db_summary_path = package.write_json("longhouse/runtime-phase-service.json", _json_safe(db_summary))
        package.write_json(
            "longhouse/runtime-state.json",
            _json_safe({"state": db_summary["state"], "runtime_key": runtime_key}),
        )
        raw_events = (
            {
                "type": "runtime_phase",
                "role": "system",
                "text": f"{self.config.provider} runtime phase running",
                "provider_session_id": self._session_id(package),
                "runtime_key": runtime_key,
                "phase": "running",
            },
            {
                "type": "runtime_phase",
                "role": "system",
                "text": f"{self.config.provider} runtime phase idle",
                "provider_session_id": self._session_id(package),
                "runtime_key": runtime_key,
                "phase": "idle",
            },
        )
        projection = self._write_observation_projection(
            package,
            scenario="runtime_phase",
            operation="runtime_phase",
            canary="universal_runtime_phase",
            raw_events=raw_events,
            source="universal harness runtime event reducer plus canonical projection",
        )
        assertions = {
            "events_accepted": db_summary["ingest_result"]["accepted"] == 3,
            "runtime_state_created": db_summary["state"] is not None,
            "runtime_phase_idle": (db_summary["state"] or {}).get("phase") == "idle",
            "runtime_version_advanced": int((db_summary["state"] or {}).get("runtime_version") or 0) >= 2,
        }
        status = STATUS_PASS if all(assertions.values()) else STATUS_FAIL
        operation_evidence = dict(projection.get("operation_evidence") or {})
        operation_evidence["runtime_phase"] = {
            "status": status,
            "level": "hermetic",
            "canary": "universal_runtime_phase",
            "failure_code": None if status == STATUS_PASS else "runtime_phase_projection_failed",
        }
        payload = {
            **projection,
            "status": status,
            "scenario": "runtime_phase",
            "db_path": str(db_path),
            "db_summary_path": str(db_summary_path),
            "runtime_key": runtime_key,
            "runtime_state_path": str(package.path("longhouse", "runtime-state.json")),
            "runtime_state": db_summary["state"],
            "assertions": assertions,
            "operation_evidence": operation_evidence,
        }
        if status != STATUS_PASS:
            payload["failure_code"] = "runtime_phase_projection_failed"
            payload["message"] = "Longhouse did not project the runtime phase reducer state as expected."
        package.write_json("assertions/runtime_phase.json", payload)
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
        if self.config.provider != "opencode":
            if self.config.provider == "claude":
                return self._run_claude_managed_session_e2e(package)
            if self.config.provider == "codex":
                return self._run_codex_managed_session_e2e(package)
            if self.config.provider == "antigravity":
                return self._run_antigravity_managed_session_e2e(package)
            payload = self._unsupported_payload(
                "managed_session_e2e",
                "managed_session_e2e_adapter_missing",
                "No managed-session e2e adapter is implemented for this provider yet.",
            )
            package.write_json("assertions/managed_session_e2e.json", payload)
            return payload
        return self._run_opencode_managed_session_e2e(package)

    def cleanup(self, package: EvidencePackage) -> dict[str, Any]:
        payload = {"status": STATUS_PASS, "message": "MVP cleanup completed; no managed process was launched."}
        package.write_json("assertions/cleanup.json", payload)
        return payload

    def _run_opencode_managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
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

    def _run_claude_interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="claude",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        claude = _claude_control_canary(control_artifact)
        operation_evidence = claude_channel_control_operation_evidence(claude)
        raw_events = claude_channel_control_raw_events(claude)
        provider_session_id = _first_claude_control_session_id(claude) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(control_artifact.get("verdict") or "red")
        interrupt_status = str((operation_evidence.get("interrupt") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and interrupt_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "interrupt_cancel",
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or interrupt_status != STATUS_PASS:
            failure_code = control_artifact.get("failure_code") or claude.get("failure_code")
            payload["failure_code"] = failure_code or "claude_interrupt_cancel_failed"
            payload["message"] = "Claude channel interrupt canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "interrupt_cancel_db_ingest_failed"
            payload["message"] = "Claude interrupt evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def _run_claude_steer_active_turn(self, package: EvidencePackage) -> dict[str, Any]:
        payload = dict(self._run_claude_interrupt_cancel(package))
        operation_evidence = {
            str(operation): dict(evidence)
            for operation, evidence in dict(payload.get("operation_evidence") or {}).items()
            if isinstance(evidence, Mapping)
        }
        steer_status = str((operation_evidence.get("steer_active_turn") or {}).get("status") or STATUS_FAIL)
        db_status = str(((payload.get("longhouse_ingest") or {}).get("status")) or STATUS_FAIL)
        verdict = str(payload.get("provider_control_verdict") or "red")
        passed = verdict == "green" and steer_status == STATUS_PASS and db_status == STATUS_PASS
        payload["status"] = STATUS_PASS if passed else STATUS_FAIL
        payload["scenario"] = "steer_active_turn"
        if passed:
            payload.pop("failure_code", None)
            payload.pop("message", None)
        elif verdict != "green" or steer_status != STATUS_PASS:
            payload["failure_code"] = payload.get("failure_code") or "claude_steer_active_turn_failed"
            payload["message"] = "Claude channel steer canary did not pass."
        else:
            payload["failure_code"] = payload.get("failure_code") or "steer_active_turn_db_ingest_failed"
            payload["message"] = "Claude steer evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/steer_active_turn.json", payload)
        return payload

    def _run_claude_resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_DEVELOPMENT_FLAG
        from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_SERVER_NAME
        from zerg.services.claude_channel_bridge import build_claude_channel_exec_command

        provider_session_id = "11111111-1111-1111-1111-111111111111"
        longhouse_session_id = "22222222-2222-4222-8222-222222222222"
        cwd = str(package.path("workspace"))
        command = build_claude_channel_exec_command(
            provider_session_id=provider_session_id,
            longhouse_session_id=longhouse_session_id,
            cwd=cwd,
            resume=True,
            claude_command=str(self.provider_bin or self.config.binary_name),
        )
        assertions = {
            "uses_resume_flag": f"--resume {provider_session_id}" in command,
            "does_not_use_session_id_flag": f"--session-id {provider_session_id}" not in command,
            "exports_longhouse_session_id": f"LONGHOUSE_CHANNEL_SESSION_ID={longhouse_session_id}" in command,
            "exports_provider_session_id": f"LONGHOUSE_PROVIDER_SESSION_ID={provider_session_id}" in command,
            "loads_development_channel": CLAUDE_CHANNEL_DEVELOPMENT_FLAG in command,
            "loads_longhouse_channel_server": f"server:{CLAUDE_CHANNEL_SERVER_NAME}" in command,
            "changes_to_workspace": cwd in command,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/claude-resume-command.json",
            {
                "command": command,
                "provider_session_id": provider_session_id,
                "longhouse_session_id": longhouse_session_id,
                "cwd": cwd,
                "assertions": assertions,
            },
        )
        operations = {
            "reattach": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "claude_channel_resume_command_shape",
                "failure_code": None if passed else "claude_resume_command_shape_failed",
                "source": "zerg.services.claude_channel_bridge.build_claude_channel_exec_command",
            }
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "system",
                    "role": "system",
                    "text": "Claude channel resume command shape was built for an existing provider session.",
                    "provider_session_id": provider_session_id,
                    "source_canary": "claude_channel_resume_command_shape",
                    "evidence_origin": "claude_channel_bridge_command_shape",
                },
            ),
            operations=operations,
            provider_session_id=provider_session_id,
        )
        next_gate = "Promote with launch, process restart, reattach, and send against the same provider session id."
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": "resume_reattach",
                "assertions": assertions,
                "raw_resume_command_path": str(raw_path),
                "proof_scope": "claude_channel_resume_command_shape",
                "synthetic": False,
                "next": next_gate,
            }
        )
        if not passed:
            payload["failure_code"] = "claude_resume_command_shape_failed"
            payload["message"] = "Claude resume command shape did not pass."
        package.write_json("assertions/resume_reattach.json", payload)
        return payload

    def _run_codex_steer_active_turn(self, package: EvidencePackage) -> dict[str, Any]:
        os.environ.setdefault("TESTING", "1")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

        from zerg.database import initialize_database
        from zerg.database import make_engine
        from zerg.database import make_sessionmaker
        from zerg.models.agents import AgentSession
        from zerg.services import managed_local_control as control
        from zerg.session_execution_home import ManagedSessionTransport
        from zerg.session_execution_home import SessionExecutionHome

        db_path = package.path("longhouse", "codex-steer-dispatch.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{db_path}")
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)
        now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        steer_text = "Longhouse universal Codex steer transport proof."
        attachment_id = "11111111-1111-1111-1111-111111111111"
        blob_url = f"/api/agents/sessions/codex-steer/inputs/1/attachments/{attachment_id}/blob"
        attachments = [
            {
                "id": attachment_id,
                "mime_type": "image/png",
                "sha256": "a" * 64,
                "blob_url": blob_url,
            }
        ]
        calls: list[dict[str, Any]] = []
        codex_transport = ManagedSessionTransport.CODEX_APP_SERVER.value

        async def fake_dispatch(**kwargs: Any) -> SimpleNamespace:
            calls.append(
                {
                    "owner_id": kwargs.get("owner_id"),
                    "timeout_secs": kwargs.get("timeout_secs"),
                    "command_type": kwargs.get("command_type"),
                    "payload": kwargs.get("payload"),
                    "commis_id": kwargs.get("commis_id"),
                    "run_id": kwargs.get("run_id"),
                    "provider": getattr(kwargs.get("session"), "provider", None),
                    "managed_transport": getattr(kwargs.get("session"), "managed_transport", None),
                }
            )
            return SimpleNamespace(
                ok=True,
                transport="engine_channel",
                data={"exit_code": 0, "stdout": "", "stderr": ""},
                error=None,
            )

        original_dispatch = control.dispatch_managed_control_command
        original_transport_error = control._managed_control_transport_error
        control.dispatch_managed_control_command = fake_dispatch
        control._managed_control_transport_error = lambda *_args, **_kwargs: None
        try:
            with session_factory() as db:
                session = AgentSession(
                    provider="codex",
                    environment="test",
                    project="universal-agent-harness",
                    device_id="universal-harness",
                    cwd=str(package.path("workspace")),
                    started_at=now - timedelta(minutes=5),
                    last_activity_at=now,
                    provider_session_id="codex-steer-transport-session",
                    user_messages=1,
                    assistant_messages=1,
                    execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
                    managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
                    managed_session_name="codex-steer-transport-proof",
                )
                db.add(session)
                db.flush()
                result = asyncio.run(
                    control.steer_text_to_managed_local_session(
                        db=db,
                        owner_id=1,
                        session=session,
                        text=steer_text,
                        commis_id="universal-codex-steer",
                        attachments=attachments,
                    )
                )
        finally:
            control.dispatch_managed_control_command = original_dispatch
            control._managed_control_transport_error = original_transport_error

        request = calls[0] if calls else {}
        expected_payload = {"text": steer_text, "intent": "steer", "attachments": attachments}
        assertions = {
            "command_dispatched": bool(calls),
            "command_type_matches": request.get("command_type") == "session.steer_text",
            "payload_matches": request.get("payload") == expected_payload,
            "provider_is_codex": request.get("provider") == "codex",
            "transport_is_codex_app_server": request.get("managed_transport") == codex_transport,
            "result_ok": result.ok is True,
            "exit_code_zero": result.exit_code == 0,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/codex-steer-dispatch.json",
            {
                "db_path": str(db_path),
                "calls": calls,
                "result": {
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "error": result.error,
                },
                "assertions": assertions,
            },
        )
        operations = {
            "steer_active_turn": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "codex_managed_local_steer_dispatch",
                "failure_code": None if passed else "codex_steer_dispatch_failed",
                "source": "zerg.services.managed_local_control.steer_text_to_managed_local_session",
            }
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "user",
                    "role": "user",
                    "text": steer_text,
                    "provider_session_id": "codex-steer-transport-session",
                    "source_canary": "codex_managed_local_steer_dispatch",
                    "intent": "steer",
                    "evidence_origin": "managed_local_control_transport_proof",
                },
            ),
            operations=operations,
            provider_session_id="codex-steer-transport-session",
        )
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": "steer_active_turn",
                "assertions": assertions,
                "raw_steer_dispatch_path": str(raw_path),
                "proof_scope": "codex_managed_local_steer_dispatch",
                "synthetic": False,
            }
        )
        if not passed:
            payload["failure_code"] = "codex_steer_dispatch_failed"
            payload["message"] = "Codex managed-local steer dispatch did not pass."
        package.write_json("assertions/steer_active_turn.json", payload)
        return payload

    def _run_opencode_interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
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

    def _run_opencode_resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "resume_reattach")
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
            "scenario": "resume_reattach",
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if live_verdict != "green" or reattach_status != STATUS_PASS:
            payload["failure_code"] = live_artifact.get("failure_code") or "opencode_resume_reattach_failed"
            payload["message"] = "OpenCode process-restart reattach canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "resume_reattach_db_ingest_failed"
            payload["message"] = "OpenCode reattach evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/resume_reattach.json", payload)
        return payload

    def _run_claude_provider_live_projection(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        assertion_name: str,
        require_operation: str | None = None,
    ) -> dict[str, Any]:
        binary, source = self._resolve_binary()
        if binary is None:
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "provider_binary_not_found",
                "message": f"claude binary was not found for {scenario}",
                "binary_source": source,
            }
            package.write_json(f"assertions/{assertion_name}.json", payload)
            return payload

        from zerg.qa.provider_live_canary import run_provider_live_canary

        live_evidence_root = package.path("raw", "provider-live-evidence")
        live_artifact_path = package.path("raw", "provider-live-canary.json")
        live_artifact = run_provider_live_canary(
            {
                "provider": "claude",
                "provider_bin": str(binary),
                "artifact": live_artifact_path,
                "evidence_root": live_evidence_root,
                "wait_ready_secs": 15.0,
                "json": False,
            }
        )
        package.write_json("raw/provider-live-canary-inline.json", live_artifact)
        operation_evidence = claude_provider_live_operation_evidence(live_artifact)
        provider_session_id = str(live_artifact.get("provider_session_id") or self._session_id(package))
        raw_events = claude_provider_live_raw_events(live_artifact, provider_session_id=provider_session_id)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        live_verdict = str(live_artifact.get("verdict") or "red")
        db_verdict = str(db_ingest.get("status") or STATUS_FAIL)
        status = STATUS_PASS if live_verdict == "green" and db_verdict == STATUS_PASS else STATUS_FAIL
        if live_verdict == "yellow" and db_verdict == STATUS_PASS:
            status = STATUS_BLOCKED
        payload = {
            **projection,
            "status": status,
            "scenario": scenario,
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if live_verdict == "red":
            payload["failure_code"] = live_artifact.get("failure_code") or "provider_live_canary_failed"
            payload["message"] = "Claude provider-live no-token canary did not pass."
        elif live_verdict == "yellow":
            payload["failure_code"] = live_artifact.get("failure_code") or "claude_provider_live_unconfirmed"
            payload["message"] = "Claude provider-live no-token canary is recognized but not fully confirmed."
        elif db_verdict != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or f"{scenario}_db_ingest_failed"
            payload["message"] = "Claude provider-live evidence did not pass Longhouse DB ingest assertions."
        if require_operation and status == STATUS_PASS:
            operation_status = str((operation_evidence.get(require_operation) or {}).get("status") or STATUS_FAIL)
            if operation_status != STATUS_PASS:
                payload["status"] = STATUS_FAIL
                payload["failure_code"] = f"claude_{require_operation}_evidence_missing"
                message = f"Claude provider-live canary did not produce passing {require_operation} evidence."
                payload["message"] = message
        package.write_json(f"assertions/{assertion_name}.json", payload)
        return payload

    def _run_claude_managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
        return self._run_claude_provider_live_projection(
            package,
            scenario="managed_session_e2e",
            assertion_name="managed_session_e2e",
        )

    def _run_claude_launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
        return self._run_claude_provider_live_projection(
            package,
            scenario="launch_managed_session",
            assertion_name="launch_managed_session",
            require_operation="launch_local",
        )

    def _run_codex_interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "interrupt_cancel")
        if binary_error is not None:
            return binary_error

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-interrupt-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "skip_static_contract": True,
                "run_managed_live_interrupt": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)
        credentials_gap = _codex_canary_credentials_gap(canary_artifact, ("managed_live_interrupt",))
        if credentials_gap:
            return self._run_codex_interrupt_dispatch_proof(
                package,
                credentials_gap=credentials_gap,
                canary_artifact_path=canary_artifact_path,
                canary_evidence_root=canary_evidence_root,
                source_artifact_kind=canary_artifact.get("artifact_kind"),
            )

        operation_evidence = self._operation_evidence_map(canary_artifact.get("operation_evidence"))
        raw_events = codex_interrupt_cancel_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        interrupt_status = str((operation_evidence.get("interrupt") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and interrupt_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "interrupt_cancel",
            "provider_version": canary_artifact.get("codex_version") or canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or interrupt_status != STATUS_PASS:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_interrupt_cancel_failed"
            payload["message"] = "Codex managed live interrupt canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "interrupt_cancel_db_ingest_failed"
            payload["message"] = "Codex interrupt evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def _run_codex_interrupt_dispatch_proof(
        self,
        package: EvidencePackage,
        *,
        credentials_gap: list[str],
        canary_artifact_path: Path,
        canary_evidence_root: Path,
        source_artifact_kind: object,
    ) -> dict[str, Any]:
        os.environ.setdefault("TESTING", "1")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

        from zerg.database import initialize_database
        from zerg.database import make_engine
        from zerg.database import make_sessionmaker
        from zerg.models.agents import AgentSession
        from zerg.services import managed_local_control as control
        from zerg.session_execution_home import ManagedSessionTransport
        from zerg.session_execution_home import SessionExecutionHome

        db_path = package.path("longhouse", "codex-interrupt-dispatch.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{db_path}")
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)
        now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        calls: list[dict[str, Any]] = []
        codex_transport = ManagedSessionTransport.CODEX_APP_SERVER.value

        async def fake_dispatch(**kwargs: Any) -> SimpleNamespace:
            calls.append(
                {
                    "owner_id": kwargs.get("owner_id"),
                    "timeout_secs": kwargs.get("timeout_secs"),
                    "command_type": kwargs.get("command_type"),
                    "payload": kwargs.get("payload"),
                    "commis_id": kwargs.get("commis_id"),
                    "run_id": kwargs.get("run_id"),
                    "provider": getattr(kwargs.get("session"), "provider", None),
                    "managed_transport": getattr(kwargs.get("session"), "managed_transport", None),
                }
            )
            return SimpleNamespace(
                ok=True,
                transport="engine_channel",
                data={"exit_code": 0, "stdout": "interrupted", "stderr": ""},
                error=None,
            )

        original_dispatch = control.dispatch_managed_control_command
        original_transport_error = control._managed_control_transport_error
        control.dispatch_managed_control_command = fake_dispatch
        control._managed_control_transport_error = lambda *_args, **_kwargs: None
        try:
            with session_factory() as db:
                session = AgentSession(
                    provider="codex",
                    environment="test",
                    project="universal-agent-harness",
                    device_id="universal-harness",
                    cwd=str(package.path("workspace")),
                    started_at=now - timedelta(minutes=5),
                    last_activity_at=now,
                    provider_session_id="codex-interrupt-transport-session",
                    user_messages=1,
                    assistant_messages=1,
                    execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
                    managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
                    managed_session_name="codex-interrupt-transport-proof",
                )
                db.add(session)
                db.flush()
                result = asyncio.run(
                    control.interrupt_managed_local_session(
                        db=db,
                        owner_id=1,
                        session=session,
                        commis_id="universal-codex-interrupt",
                    )
                )
        finally:
            control.dispatch_managed_control_command = original_dispatch
            control._managed_control_transport_error = original_transport_error

        request = calls[0] if calls else {}
        assertions = {
            "command_dispatched": bool(calls),
            "command_type_matches": request.get("command_type") == "session.interrupt",
            "payload_empty": request.get("payload") == {},
            "provider_is_codex": request.get("provider") == "codex",
            "transport_is_codex_app_server": request.get("managed_transport") == codex_transport,
            "result_ok": result.ok is True,
            "exit_code_zero": result.exit_code == 0,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/codex-interrupt-dispatch.json",
            {
                "db_path": str(db_path),
                "credentials_gap": credentials_gap,
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "calls": calls,
                "result": {
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "error": result.error,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                "assertions": assertions,
            },
        )
        operations = {
            "interrupt": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "codex_managed_local_interrupt_dispatch",
                "failure_code": None if passed else "codex_interrupt_dispatch_failed",
                "source": "zerg.services.managed_local_control.interrupt_managed_local_session",
            },
            "live_interrupt_canary": {
                "status": STATUS_BLOCKED,
                "level": "live_token_required",
                "canary": "managed_live_interrupt",
                "failure_code": "codex_managed_bridge_credentials_missing",
            },
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "system",
                    "role": "system",
                    "text": "Codex managed-local interrupt dispatch command completed.",
                    "provider_session_id": "codex-interrupt-transport-session",
                    "source_canary": "codex_managed_local_interrupt_dispatch",
                    "evidence_origin": "managed_local_control_transport_proof",
                },
            ),
            operations=operations,
            provider_session_id="codex-interrupt-transport-session",
        )
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": "interrupt_cancel",
                "assertions": assertions,
                "raw_interrupt_dispatch_path": str(raw_path),
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "source_artifact_kind": source_artifact_kind,
                "missing_live_credentials": credentials_gap,
                "proof_scope": "codex_managed_local_interrupt_dispatch",
                "synthetic": False,
                "operation_evidence": operations,
                "next": "Promote with managed-live Codex interrupt canary when Runtime Host credentials are present.",
            }
        )
        if not passed:
            payload["failure_code"] = "codex_interrupt_dispatch_failed"
            payload["message"] = "Codex interrupt dispatch proof did not pass."
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def _run_codex_tool_call_result(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "tool_call_result")
        if binary_error is not None:
            return binary_error

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-real-tool-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "skip_static_contract": True,
                "run_real_tool": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)

        operation_evidence = codex_tool_call_result_operation_evidence(canary_artifact)
        raw_events = codex_tool_call_result_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        tool_status = str((operation_evidence.get("tool_call_result") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and tool_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "tool_call_result",
            "provider_version": canary_artifact.get("codex_version") or canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or tool_status != STATUS_PASS:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_tool_call_result_failed"
            payload["message"] = "Codex real-tool call/result canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "tool_call_result_db_ingest_failed"
            payload["message"] = "Codex real-tool call/result evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/tool_call_result.json", payload)
        return payload

    def _run_opencode_tool_call_result(self, package: EvidencePackage) -> dict[str, Any]:
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

    def _run_claude_live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "live_token_streaming")
        if binary_error is not None:
            return binary_error

        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="claude",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
            extra_args=["--claude-run-real-print"],
            extra_env={"LONGHOUSE_CLAUDE_BIN": str(binary)},
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        claude = _claude_control_canary(control_artifact)
        operation_evidence = claude_real_print_operation_evidence(claude)
        raw_events = claude_real_print_raw_events(claude)
        provider_session_id = _first_claude_control_session_id(claude) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(control_artifact.get("verdict") or "red")
        live_status = str((operation_evidence.get("live_token_behavior") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and live_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "live_token_streaming",
            "provider_version": claude.get("provider_version"),
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or live_status != STATUS_PASS:
            failure_code = control_artifact.get("failure_code") or claude.get("failure_code")
            payload["failure_code"] = failure_code or "claude_live_token_streaming_failed"
            payload["message"] = "Claude real-print live-token canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "live_token_streaming_db_ingest_failed"
            payload["message"] = "Claude live-token evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload

    def _run_codex_live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "live_token_streaming")
        if binary_error is not None:
            return binary_error

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-live-token-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "skip_static_contract": True,
                "run_managed_live_send": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)
        credentials_gap = _codex_canary_credentials_gap(canary_artifact, ("managed_live_send",))
        if credentials_gap:
            payload = {
                "status": STATUS_UNSUPPORTED_GAP,
                "scenario": "live_token_streaming",
                "failure_code": "codex_managed_bridge_credentials_missing",
                "message": "Codex live_token_streaming requires Runtime Host credentials.",
                "missing": credentials_gap,
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "source_artifact_kind": canary_artifact.get("artifact_kind"),
                "synthetic": False,
                "operation_evidence": {
                    "send_input": {
                        "status": STATUS_UNSUPPORTED_GAP,
                        "level": "live_token_required",
                        "canary": "managed_live_send",
                        "failure_code": "codex_managed_bridge_credentials_missing",
                    },
                    "live_token_behavior": {
                        "status": STATUS_UNSUPPORTED_GAP,
                        "level": "live_token_required",
                        "canary": "managed_live_send",
                        "failure_code": "codex_managed_bridge_credentials_missing",
                    },
                },
            }
            package.write_json("assertions/live_token_streaming.json", payload)
            return payload

        operation_evidence = codex_live_token_streaming_operation_evidence(canary_artifact)
        raw_events = codex_live_token_streaming_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        live_status = str((operation_evidence.get("live_token_behavior") or {}).get("status") or STATUS_FAIL)
        send_status = str((operation_evidence.get("send_input") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = all(
            (
                verdict == "green",
                live_status == STATUS_PASS,
                send_status == STATUS_PASS,
                db_status == STATUS_PASS,
            )
        )
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "live_token_streaming",
            "provider_version": canary_artifact.get("codex_version") or canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or live_status != STATUS_PASS or send_status != STATUS_PASS:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_live_token_streaming_failed"
            payload["message"] = "Codex managed live-send canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "live_token_streaming_db_ingest_failed"
            payload["message"] = "Codex live-token evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload

    def _run_opencode_live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
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
        if verdict != "green" or live_status != STATUS_PASS or run_once_status != STATUS_PASS:
            failure_code = control_artifact.get("failure_code") or opencode.get("failure_code")
            payload["failure_code"] = failure_code or "opencode_live_token_streaming_failed"
            payload["message"] = "OpenCode real-print canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "live_token_streaming_db_ingest_failed"
            payload["message"] = "OpenCode live-token evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload

    def _run_antigravity_live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
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

    def _run_codex_managed_session_canary_projection(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        assertion_name: str,
        require_operation: str | None = None,
    ) -> dict[str, Any]:
        binary, source = self._resolve_binary()
        if binary is None:
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "provider_binary_not_found",
                "message": f"codex binary was not found for {scenario}",
                "binary_source": source,
            }
            package.write_json(f"assertions/{assertion_name}.json", payload)
            return payload

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-release-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "run_managed_tui_attach": True,
                "run_detached_ui": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)
        operation_evidence = self._operation_evidence_map(canary_artifact.get("operation_evidence"))
        raw_events = codex_provider_release_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        credentials_gap = _codex_managed_bridge_credentials_gap(canary_artifact)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        payload = {
            **projection,
            "status": STATUS_PASS if verdict == "green" and db_status == STATUS_PASS else STATUS_FAIL,
            "scenario": scenario,
            "provider_version": canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if credentials_gap:
            if scenario == "resume_reattach":
                return self._run_codex_resume_attach_command_proof(
                    package,
                    credentials_gap=credentials_gap,
                    canary_artifact_path=canary_artifact_path,
                    canary_evidence_root=canary_evidence_root,
                    source_artifact_kind=canary_artifact.get("artifact_kind"),
                )
            payload["status"] = STATUS_UNSUPPORTED_GAP
            payload["failure_code"] = "codex_managed_bridge_credentials_missing"
            payload["message"] = f"Codex {scenario} requires Runtime Host credentials."
            payload["missing"] = credentials_gap
        elif verdict != "green":
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_provider_release_canary_failed"
            payload["message"] = "Codex provider release canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or f"{scenario}_db_ingest_failed"
            payload["message"] = "Codex canary evidence did not pass Longhouse DB ingest assertions."
        if require_operation and not credentials_gap and verdict == "green" and db_status == STATUS_PASS:
            operation_status = str((operation_evidence.get(require_operation) or {}).get("status") or STATUS_FAIL)
            if operation_status != STATUS_PASS:
                payload["status"] = STATUS_FAIL
                payload["failure_code"] = f"codex_{require_operation}_evidence_missing"
                payload["message"] = f"Codex canary did not produce passing {require_operation} evidence."
        package.write_json(f"assertions/{assertion_name}.json", payload)
        return payload

    def _run_codex_resume_attach_command_proof(
        self,
        package: EvidencePackage,
        *,
        credentials_gap: list[str],
        canary_artifact_path: Path,
        canary_evidence_root: Path,
        source_artifact_kind: object,
    ) -> dict[str, Any]:
        from zerg.services.managed_local_transport import build_managed_local_attach_command
        from zerg.session_execution_home import ManagedSessionTransport

        longhouse_session_id = "33333333-3333-4333-8333-333333333333"
        session = SimpleNamespace(
            id=longhouse_session_id,
            managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
        )
        command = build_managed_local_attach_command(session=session)
        assertions = {
            "command_built": command is not None,
            "uses_engine_bridge_attach": "codex-bridge attach" in str(command or ""),
            "uses_longhouse_session_id": f"--session-id {longhouse_session_id}" in str(command or ""),
            "requires_longhouse_engine": "command -v longhouse-engine" in str(command or ""),
            "requires_codex": "command -v codex" in str(command or ""),
            "execs_engine": 'exec "$engine" codex-bridge attach' in str(command or ""),
            "uses_zsh_shell": str(command or "").startswith("zsh -lc "),
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/codex-reattach-command.json",
            {
                "command": command,
                "longhouse_session_id": longhouse_session_id,
                "credentials_gap": credentials_gap,
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "assertions": assertions,
            },
        )
        operations = {
            "reattach": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "codex_managed_local_attach_command_shape",
                "failure_code": None if passed else "codex_reattach_command_shape_failed",
                "source": "zerg.services.managed_local_transport.build_managed_local_attach_command",
            },
            "live_reattach_canary": {
                "status": STATUS_BLOCKED,
                "level": "live_no_token",
                "canary": "managed_tui_attach",
                "failure_code": "codex_managed_bridge_credentials_missing",
            },
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "system",
                    "role": "system",
                    "text": "Codex managed-local reattach command shape was built.",
                    "provider_session_id": longhouse_session_id,
                    "source_canary": "codex_managed_local_attach_command_shape",
                    "evidence_origin": "managed_local_transport_command_shape",
                },
            ),
            operations=operations,
            provider_session_id=longhouse_session_id,
        )
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": "resume_reattach",
                "assertions": assertions,
                "raw_reattach_command_path": str(raw_path),
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "source_artifact_kind": source_artifact_kind,
                "missing_live_credentials": credentials_gap,
                "proof_scope": "codex_managed_local_attach_command_shape",
                "synthetic": False,
                "operation_evidence": operations,
                "next": "Promote with managed Codex process restart and same-thread reattach proof.",
            }
        )
        if not passed:
            payload["failure_code"] = "codex_reattach_command_shape_failed"
            payload["message"] = "Codex reattach command shape proof did not pass."
        package.write_json("assertions/resume_reattach.json", payload)
        return payload

    def _run_codex_managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
        return self._run_codex_managed_session_canary_projection(
            package,
            scenario="managed_session_e2e",
            assertion_name="managed_session_e2e",
        )

    def _run_codex_resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        return self._run_codex_managed_session_canary_projection(
            package,
            scenario="resume_reattach",
            assertion_name="resume_reattach",
            require_operation="reattach",
        )

    def _run_antigravity_launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
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

    def _unsupported_payload(self, scenario: str, failure_code: str, message: str) -> dict[str, Any]:
        next_step = "Promote through a provider adapter that can run this scenario "
        next_step += "without spending tokens or mutating external state."
        return {
            "status": STATUS_UNSUPPORTED_GAP,
            "scenario": scenario,
            "failure_code": failure_code,
            "message": message,
            "next": next_step,
        }

    def _build_action_matrix_rows(
        self,
        *,
        package: EvidencePackage,
        probe: Mapping[str, Any],
        files: Iterable[str],
    ) -> list[dict[str, Any]]:
        file_list = list(files)
        rows: list[dict[str, Any]] = []
        for action in ACTION_DEFINITIONS:
            rows.append(self.action_result(package, action, probe=probe, files=file_list))
        return rows

    def _session_id(self, package: EvidencePackage) -> str:
        return f"universal-{self.config.provider}-{package.scenario}"

    def _write_projection_surface(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        operation: str,
        canary: str,
    ) -> dict[str, Any]:
        operations = {
            operation: {
                "status": STATUS_PASS,
                "level": "hermetic",
                "canary": canary,
                "source": "universal harness canonical event projection",
            },
            "transcript_binding": {
                "status": STATUS_PASS,
                "level": "hermetic",
                "canary": canary,
                "source": "universal harness canonical event/session projection",
            },
        }
        payload = self._write_session_projection(
            package,
            raw_events=default_projection_rows(),
            operations=operations,
        )
        payload["scenario"] = scenario
        return payload

    def _write_message_exchange(
        self,
        package: EvidencePackage,
        *,
        prompt: str,
        scenario: str,
        operation: str,
        canary: str,
        level: str,
        source: str,
    ) -> dict[str, Any]:
        raw_events = (
            {
                "type": "user",
                "role": "user",
                "text": prompt,
                "session_id": self._session_id(package),
            },
            {
                "type": "assistant",
                "role": "assistant",
                "text": "LONGHOUSE UNIVERSAL HARNESS",
                "session_id": self._session_id(package),
                "synthetic": True,
            },
        )
        operations = {
            operation: {
                "status": STATUS_PASS,
                "level": level,
                "canary": canary,
                "source": source,
            },
            "transcript_binding": {
                "status": STATUS_PASS,
                "level": "hermetic",
                "canary": canary,
                "source": "universal harness canonical event/session projection",
            },
        }
        payload = self._write_session_projection(package, raw_events=raw_events, operations=operations)
        payload["scenario"] = scenario
        return payload

    def _write_session_projection(
        self,
        package: EvidencePackage,
        *,
        raw_events: Iterable[Mapping[str, Any]],
        operations: Mapping[str, Mapping[str, Any]],
        provider_session_id: str | None = None,
    ) -> dict[str, Any]:
        rows = [dict(row) for row in raw_events]
        raw_path = package.write_text(
            "events/provider-raw-events.jsonl",
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        )
        canonical: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            canonical.append(canonical_event_from_fixture(row, provider=self.config.provider, index=index))
        canonical_path = package.write_text(
            "events/canonical-longhouse-events.jsonl",
            "\n".join(json.dumps(row, sort_keys=True) for row in canonical) + "\n",
        )
        session_id = provider_session_id or self._session_id(package)
        session_projection = {
            **project_session(canonical, provider=self.config.provider),
            "provider_session_id": session_id,
            "operation_statuses": dict(operations),
        }
        timeline_projection = project_timeline(canonical)
        package.write_json("longhouse/session-projection.json", session_projection)
        package.write_json("longhouse/timeline-projection.json", timeline_projection)
        return {
            "status": STATUS_PASS,
            "provider_session_id": session_id,
            "raw_event_count": len(rows),
            "canonical_event_count": len(canonical),
            "raw_events_path": str(raw_path),
            "canonical_events_path": str(canonical_path),
            "session_projection_path": str(package.path("longhouse", "session-projection.json")),
            "timeline_projection_path": str(package.path("longhouse", "timeline-projection.json")),
            "operation_evidence": dict(operations),
        }

    @staticmethod
    def _operation_evidence_map(source: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        """Coerce a canary/db-ingest ``operation_evidence`` blob into plain dicts."""
        return {str(operation): dict(evidence) for operation, evidence in dict(source or {}).items() if isinstance(evidence, Mapping)}

    def _project_ingest_and_merge(
        self,
        package: EvidencePackage,
        *,
        operation_evidence: dict[str, dict[str, Any]],
        raw_events: list[dict[str, Any]],
        provider_session_id: str | None,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
        """Run the shared projection + Longhouse DB ingest tail.

        Every provider control scenario writes the session projection, ingests the
        same canonical events into the throwaway Longhouse DB, merges the ingest's
        ``operation_evidence`` back over the canary's, and patches the on-disk
        session projection with the merged statuses. Returns the projection summary,
        the merged operation evidence, and the raw db-ingest result.
        """
        projection = self._write_session_projection(
            package,
            raw_events=raw_events,
            operations=operation_evidence,
            provider_session_id=provider_session_id,
        )
        db_ingest = ingest_canonical_events_into_longhouse_db(
            package=package,
            provider=self.config.provider,
            rows=raw_events,
            provider_session_id=provider_session_id,
        )
        operation_evidence.update(self._operation_evidence_map(db_ingest.get("operation_evidence")))
        session_projection_path = package.path("longhouse", "session-projection.json")
        try:
            session_projection = json.loads(session_projection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            session_projection = {}
        if isinstance(session_projection, dict):
            session_projection["operation_statuses"] = operation_evidence
            package.write_json("longhouse/session-projection.json", session_projection)
        return projection, operation_evidence, db_ingest

    @staticmethod
    def _longhouse_ingest_block(db_ingest: Mapping[str, Any]) -> dict[str, Any]:
        """The ``longhouse_ingest`` payload sub-dict shared by control scenarios."""
        return {
            "status": str(db_ingest.get("status") or STATUS_FAIL),
            "failure_code": db_ingest.get("failure_code"),
            "db_snapshot_path": db_ingest.get("db_snapshot_path"),
            "session_projection_path": db_ingest.get("session_projection_path"),
            "timeline_projection_path": db_ingest.get("timeline_projection_path"),
        }

    def _require_binary(self, package: EvidencePackage, scenario: str) -> tuple[Path | None, dict[str, Any] | None]:
        """Resolve the provider binary or write+return the standard not-found payload.

        Returns ``(binary, None)`` on success and ``(None, payload)`` when the binary
        is missing, having already persisted ``assertions/<scenario>.json``.
        """
        binary, source = self._resolve_binary()
        if binary is not None:
            return binary, None
        payload = {
            "status": STATUS_FAIL,
            "failure_code": "provider_binary_not_found",
            "message": f"{self.config.binary_name} binary was not found for {scenario}",
            "binary_source": source,
        }
        package.write_json(f"assertions/{scenario}.json", payload)
        return None, payload

    def _resolve_binary(self) -> tuple[Path | None, str]:
        if self.provider_bin is not None:
            path = self.provider_bin.expanduser()
            return (path, "provider_bin") if path.is_file() else (None, "provider_bin_missing")
        if self.config.binary_env:
            raw = os.environ.get(self.config.binary_env)
            if raw:
                path = Path(raw).expanduser()
                return (path, self.config.binary_env) if path.is_file() else (None, f"{self.config.binary_env}_missing")
        path = shutil.which(self.config.binary_name)
        return (Path(path), "PATH") if path else (None, "missing")


class ClaudeCodeHarnessAdapter(UniversalProviderAdapter):
    """Claude Code concrete adapter for the universal Longhouse action contract."""


class CodexOpenAIHarnessAdapter(UniversalProviderAdapter):
    """Codex/OpenAI concrete adapter for the universal Longhouse action contract."""


class OpenCodeHarnessAdapter(UniversalProviderAdapter):
    """OpenCode concrete adapter for the universal Longhouse action contract."""


class AntigravityHarnessAdapter(UniversalProviderAdapter):
    """Antigravity concrete adapter for the universal Longhouse action contract."""


ADAPTER_CLASS_BY_PROVIDER: Mapping[str, type[UniversalProviderAdapter]] = {
    "claude": ClaudeCodeHarnessAdapter,
    "codex": CodexOpenAIHarnessAdapter,
    "opencode": OpenCodeHarnessAdapter,
    "antigravity": AntigravityHarnessAdapter,
}


def adapter_snapshot(config: AdapterConfig) -> dict[str, Any]:
    return {
        "provider": config.provider,
        "binary_name": config.binary_name,
        "binary_env": config.binary_env,
        "capabilities": list(config.capabilities),
        "profiles": list(config.profiles),
        "methods": list(config.methods),
        "real_managed_session_e2e": config.real_managed_session_e2e,
    }


def _status_counts(statuses: Iterable[str]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for status in statuses:
        key = status if status in counts else STATUS_FAIL
        counts[key] += 1
    return {key: value for key, value in counts.items() if value}


def _value_counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "")
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _contract_snapshot(provider: str) -> dict[str, Any] | None:
    contract = contract_for_provider(provider)
    if contract is None:
        return None
    return {
        "provider": contract.provider,
        "managed_transport": contract.managed_transport.value,
        "control_plane": contract.control_plane,
        "control_planes": list(contract.control_planes),
        "machine_control_supports": list(contract.machine_control_supports),
        "operations": {
            "launch_local": contract.launch_local,
            "launch_remote": contract.launch_remote,
            "run_once": contract.run_once,
            "reattach": contract.reattach,
            "send_input": contract.send_input,
            "interrupt": contract.interrupt,
            "steer_active_turn": contract.steer_active_turn,
            "terminate": contract.terminate,
            "tail_output": contract.tail_output,
            "runtime_phase": contract.runtime_phase,
            "transcript_binding": contract.transcript_binding,
            "can_resume": contract.can_resume,
        },
        "operation_evidence": {key: dict(value) for key, value in contract.operation_evidence.items()},
    }


def _action_support(provider: str, action: ActionDefinition, contract: Any) -> tuple[bool, str]:
    if action.support_kind == "harness":
        return True, "universal_harness"
    if action.support_kind == "release_proof":
        return True, "provider_release_proof"
    if action.support_kind == "longhouse_db":
        return True, "longhouse_db"
    if contract is None:
        return False, "managed_provider_contract_missing"
    if action.support_kind == "contract_bool":
        operation = str(action.contract_operation or "")
        return bool(getattr(contract, operation, False)), f"contract.{operation}"
    if action.support_kind == "session_identity":
        supported = bool(contract.launch_local or contract.reattach or contract.can_resume)
        return supported, "contract.launch_local_or_reattach"
    if action.support_kind == "pause_request":
        capability = f"{provider}.answer_pause"
        runtime_pause_supported = bool(contract.runtime_phase and contract.transcript_binding)
        supported = capability in contract.machine_control_supports or runtime_pause_supported
        return supported, "machine_control.answer_pause_or_runtime_pause_projection"
    if action.support_kind == "machine_capability:answer_pause":
        return f"{provider}.answer_pause" in contract.machine_control_supports, "machine_control.answer_pause"
    if action.support_kind == "tool_result":
        return bool(contract.transcript_binding), "contract.transcript_binding"
    if action.support_kind == "external_event_channel":
        if provider == "claude":
            return True, "provider_live.claude_development_channel"
        if provider == "antigravity":
            return True, "provider_control.antigravity_hook_inbox"
        return False, "external_event_channel_unsupported"
    if action.support_kind == "permission_prompt":
        return provider in {"claude", "codex", "opencode"}, "provider_permission_prompt_surface"
    return False, f"unknown_support_kind:{action.support_kind}"


def _provider_answer_pause_supported(provider: str) -> bool:
    contract = contract_for_provider(provider)
    if contract is None:
        return False
    return f"{provider}.answer_pause" in contract.machine_control_supports


def _provider_pause_tool_name(provider: str) -> str:
    if provider == "claude":
        return "AskUserQuestion"
    if provider == "codex":
        return "requestUserInput"
    if provider == "opencode":
        return "opencode_pause_request"
    if provider == "antigravity":
        return "antigravity_pause_request"
    return "structured_question"


def _action_implementation_kind(
    *,
    action: ActionDefinition,
    support: bool,
    contract_evidence: Mapping[str, Any],
) -> str:
    if not support:
        return "typed_unsupported_gap"
    if action.action_id == "provider_identity":
        return "provider_probe"
    if action.action_id in {
        "raw_evidence_capture",
        "parse_normalize",
        "session_projection",
        "timeline_projection",
        "multi_turn_continuity",
        "crash_timeout_cleanup",
    }:
        return "universal_harness_projection"
    if action.action_id == "external_event_channel":
        return "provider_control_canary"
    if action.action_id == "db_ingest":
        return "longhouse_db_ingest"
    if action.action_id in {"baseline_compare", "old_new_release_diff"}:
        return "provider_release_proof_diff"
    if action.action_id in {"pause_request_detect", "answer_pause_request"}:
        return "universal_pause_request_service"
    if action.action_id == "permission_prompt":
        return "typed_blocked_gap"
    if action.action_id == "tool_call_result":
        return "derived_longhouse_surface"
    if contract_evidence:
        return "managed_provider_contract"
    return "typed_blocked_gap"


def _action_status(
    *,
    action: ActionDefinition,
    support: bool,
    support_reason: str,
    contract_evidence: Mapping[str, Any],
    provider: str,
    probe: Mapping[str, Any],
    files: list[str],
    package: EvidencePackage,
) -> dict[str, Any]:
    if not support:
        return {
            "status": STATUS_UNSUPPORTED_GAP,
            "failure_code": f"{action.action_id}_unsupported",
            "message": f"{provider} does not currently support {action.action_id}.",
            "proof_scope": support_reason,
            "next": "Leave unsupported unless the provider exposes stable semantics and Longhouse adds a contract row.",
        }

    if action.action_id == "provider_identity":
        if probe.get("status") == STATUS_PASS:
            return {
                "status": STATUS_PASS,
                "evidence_level": "live_no_token",
                "proof_scope": "version_command",
                "canary": "universal_probe_identity",
                "raw_artifacts": [str(package.path("raw", "version-command.json"))],
            }
        return {
            "status": STATUS_FAIL,
            "failure_code": str(probe.get("failure_code") or "provider_identity_failed"),
            "message": str(probe.get("message") or "Provider identity probe failed."),
            "proof_scope": "version_command",
        }

    harness_pass_actions = {
        "raw_evidence_capture": ("hermetic", "universal_collect_raw_evidence", "universal_harness_file_manifest"),
        "parse_normalize": ("hermetic", "universal_parse_ingest_project", "universal_harness_parser_projection"),
        "session_projection": ("hermetic", "universal_session_projection", "universal_harness_projection"),
        "timeline_projection": ("hermetic", "universal_timeline_projection", "universal_harness_projection"),
        "multi_turn_continuity": ("hermetic", "universal_multi_turn_continuity", "universal_harness_projection"),
        "crash_timeout_cleanup": ("hermetic", "universal_crash_timeout_cleanup", "universal_harness_projection"),
    }
    if action.action_id in harness_pass_actions:
        level, canary, scope = harness_pass_actions[action.action_id]
        return {
            "status": STATUS_PASS,
            "evidence_level": level,
            "proof_scope": scope,
            "canary": canary,
            "raw_artifacts": [str(package.path(item)) for item in files],
        }

    if action.action_id == "db_ingest":
        return {
            "status": STATUS_PASS,
            "evidence_level": "hermetic",
            "proof_scope": "longhouse_sqlite_ingest",
            "source": "universal db_ingest_project scenario uses AgentsStore.ingest_session and timeline/export reads",
            "canary": "universal_db_ingest_project",
            "next": "Promote with provider-live raw evidence and hosted Runtime Host read-surface proof.",
        }

    if action.action_id == "baseline_compare":
        return {
            "status": STATUS_PASS,
            "evidence_level": "hermetic",
            "proof_scope": "provider_release_proof_baseline",
            "source": "provider-release-proof-baseline compares action_matrix and control_surface artifacts",
            "canary": "provider_release_proof_baseline_diff",
            "next": "Promote old/new release staging so the diff runs automatically for candidate provider versions.",
        }

    if action.action_id == "old_new_release_diff":
        return {
            "status": STATUS_PASS,
            "evidence_level": "artifact_diff",
            "proof_scope": "provider_release_proof_old_new",
            "source": "provider-release-proof-baseline old-new compares explicit old/new proof artifacts",
            "canary": "provider_release_proof_old_new_diff",
            "next": "Add sandboxed old/new provider install and automatic action-row diffing.",
        }

    if action.action_id in {
        "pause_request_detect",
        "answer_pause_request",
        "steer_active_turn",
        "tool_call_result",
        "external_event_channel",
        "permission_prompt",
    }:
        return _derived_action_status(action=action, provider=provider)

    level = str(contract_evidence.get("level") or "").strip()
    source = str(contract_evidence.get("source") or "").strip()
    if level and level != "none" and source:
        return {
            "status": STATUS_PASS,
            "evidence_level": level,
            "proof_scope": "managed_provider_contract",
            "source": source,
            "next": contract_evidence.get("next"),
            "canary": _contract_canary_name(source),
        }
    return {
        "status": STATUS_BLOCKED,
        "failure_code": f"{action.action_id}_proof_missing",
        "message": f"{provider} supports {action.action_id}, but no release-proof evidence source is recorded.",
        "proof_scope": "managed_provider_contract",
        "next": "Add operation evidence to managed_provider_contracts.json or implement a provider canary lane.",
    }


def _derived_action_status(*, action: ActionDefinition, provider: str) -> dict[str, Any]:
    if action.action_id == "pause_request_detect":
        return {
            "status": STATUS_PASS,
            "evidence_level": "hermetic",
            "proof_scope": "session_pause_request_projection",
            "source": "server/zerg/services/session_pause_requests.py plus session_chat pause-request API tests",
            "canary": "session_pause_request_projection_tests",
            "next": "Promote with provider-specific live structured-question canaries.",
        }
    if action.action_id == "tool_call_result":
        return {
            "status": STATUS_PASS,
            "evidence_level": "hermetic",
            "proof_scope": "shipper_parser_tool_result",
            "source": "server/tests_lite/test_shipper_parser_tool_results.py and provider parser fixtures",
            "canary": "shipper_parser_tool_results",
            "next": "Promote with live provider tool-call/result canaries per provider.",
        }
    if action.action_id == "steer_active_turn":
        if provider == "codex":
            return {
                "status": STATUS_PASS,
                "evidence_level": "hermetic",
                "proof_scope": "codex_managed_local_steer_dispatch",
                "source": "zerg.services.managed_local_control.steer_text_to_managed_local_session",
                "canary": "codex_managed_local_steer_dispatch",
                "next": "Promote with a live active-turn Codex steer canary that proves provider behavior.",
            }
        return {
            "status": STATUS_BLOCKED,
            "failure_code": "steer_active_turn_provider_canary_missing",
            "message": f"{provider} steer_active_turn behavior needs a provider-specific canary.",
            "proof_scope": "managed_control_steer",
            "next": "Add a live active-turn steer canary, or keep unsupported without a stable provider semantic.",
        }
    if action.action_id == "answer_pause_request":
        if provider in {"claude", "codex"}:
            return {
                "status": STATUS_PASS,
                "evidence_level": "hermetic",
                "proof_scope": "universal_answer_pause_dispatch",
                "source": "zerg.services.managed_local_control.answer_pause_request_on_managed_local_session",
                "canary": "universal_answer_pause_dispatch",
                "next": "Promote with a live provider-held structured-question canary.",
            }
        return {
            "status": STATUS_UNSUPPORTED_GAP,
            "failure_code": "answer_pause_request_unsupported",
            "message": f"{provider} does not expose stable answer-pause machine-control semantics.",
            "proof_scope": "machine_control.answer_pause",
            "next": "Keep unsupported until the provider exposes answer-pause semantics.",
        }
    if action.action_id == "external_event_channel":
        if provider == "claude":
            return {
                "status": STATUS_PASS,
                "evidence_level": "live_no_token",
                "proof_scope": "provider_live.claude_development_channel",
                "source": "longhouse provider-live canary --provider claude development channel contract",
                "canary": "claude_development_channels_contract",
                "next": "Promote with live channel send/receive behavior proof when available.",
            }
        return {
            "status": STATUS_PASS,
            "evidence_level": "hermetic",
            "proof_scope": "provider_control.antigravity_hook_inbox",
            "source": "scripts/qa/provider-control-e2e-canary.py Antigravity hook/inbox pre/post injection",
            "canary": "provider_control_e2e_antigravity_hook_inbox",
            "next": "Keep other providers unsupported unless they expose stable external-event semantics.",
        }
    if action.action_id == "permission_prompt":
        if provider == "codex":
            return {
                "status": STATUS_PASS,
                "evidence_level": "hermetic",
                "proof_scope": "codex_fake_app_server_permission_approval",
                "source": "engine/src/codex_app_server_canary.rs fake app-server approval request test",
                "canary": "codex_fake_app_server_permission_approval",
                "next": "Promote with a live held-permission Codex provider canary.",
            }
        if provider == "opencode":
            return {
                "status": STATUS_PASS,
                "evidence_level": "hermetic",
                "proof_scope": "opencode_bridge_permission_reply",
                "source": "zerg.cli.opencode_bridge permission-reply against a held fake permission request",
                "canary": "opencode_bridge_permission_reply",
                "next": "Promote with a live held-permission OpenCode provider canary.",
            }
        return {
            "status": STATUS_BLOCKED,
            "failure_code": "permission_prompt_canary_missing",
            "message": f"{provider} permission prompt approve/deny behavior needs a live held-permission canary.",
            "proof_scope": "provider_permission_prompt_surface",
            "next": "Add provider-held permission prompt fixtures/canaries that prove approve and deny delivery.",
        }
    message = "".join(
        [
            f"{provider} advertises {action.action_id}, ",
            "but the universal matrix has no direct canary evidence yet.",
        ]
    )
    return {
        "status": STATUS_BLOCKED,
        "failure_code": f"{action.action_id}_provider_canary_missing",
        "message": message,
        "proof_scope": "machine_control.answer_pause",
        "next": "Add a managed-session pause request e2e that creates, lists, answers, and resolves one request.",
    }


def _contract_canary_name(source: str) -> str:
    cleaned = source.split()[0].replace("/", "_").replace(".", "_").replace("-", "_")
    return cleaned[:80] or "managed_provider_contract"


def _operation_from_action_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": row.get("status"),
        "level": row.get("evidence_level"),
        "canary": row.get("canary"),
        "failure_code": row.get("failure_code"),
    }


def _action_rows_from_result(result: ScenarioResult) -> list[dict[str, Any]]:
    data = result.data or {}
    rows = data.get("actions") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _full_action_suite_coverage(
    *,
    provider: str,
    matrix_actions: list[dict[str, Any]],
    results_by_scenario: Mapping[str, ScenarioResult],
) -> list[dict[str, Any]]:
    matrix_by_action = {str(row.get("action_id")): row for row in matrix_actions}
    coverage: list[dict[str, Any]] = []
    for action in ACTION_DEFINITIONS:
        matrix = matrix_by_action.get(action.action_id)
        scenarios = ACTION_EXECUTION_SCENARIO_BY_ID.get(action.action_id, ())
        scenario_results = [results_by_scenario[name] for name in scenarios if name in results_by_scenario]
        scenario_statuses = {result.scenario: result.status for result in scenario_results}
        scenario_failure_codes = {}
        for result in scenario_results:
            if result.failure_code:
                scenario_failure_codes[result.scenario] = result.failure_code
        coverage_kind = "executable_scenario" if scenarios else "matrix_contract"
        if matrix is None:
            coverage_status = "missing"
            failure_code = "action_matrix_row_missing"
        elif scenario_results:
            coverage_status = _scenario_coverage_status(
                action_id=action.action_id,
                results=scenario_results,
            )
            failure_code = None if coverage_status == STATUS_PASS else _first_failure_code(scenario_results)
        else:
            matrix_status = str(matrix.get("status") or STATUS_FAIL)
            coverage_status = matrix_status if matrix_status in STATUSES else STATUS_FAIL
            failure_code = str(matrix.get("failure_code") or "") or None
        coverage.append(
            {
                "action_id": action.action_id,
                "provider": provider,
                "category": action.category,
                "coverage_kind": coverage_kind,
                "coverage_status": coverage_status,
                "coverage_gap_kind": _coverage_gap_kind(
                    coverage_status=coverage_status,
                    failure_code=failure_code,
                    matrix=matrix,
                ),
                "failure_code": failure_code,
                "matrix_status": matrix.get("status") if matrix else None,
                "matrix_failure_code": matrix.get("failure_code") if matrix else None,
                "matrix_support": matrix.get("support") if matrix else None,
                "matrix_support_reason": matrix.get("support_reason") if matrix else None,
                "scenario_ids": list(scenarios),
                "scenario_statuses": scenario_statuses,
                "scenario_failure_codes": scenario_failure_codes,
                "coverage_policy": _action_coverage_policy(action.action_id),
                "required_evidence": action.required_evidence,
            }
        )
    return coverage


def _coverage_gap_kind(
    *,
    coverage_status: str,
    failure_code: str | None,
    matrix: Mapping[str, Any] | None,
) -> str:
    if coverage_status == STATUS_PASS:
        return COVERAGE_GAP_PASSED
    if coverage_status == "missing":
        return COVERAGE_GAP_MISSING_COVERAGE
    if coverage_status == STATUS_NOT_APPLICABLE:
        return COVERAGE_GAP_NOT_APPLICABLE
    if coverage_status == STATUS_FLAKY:
        return COVERAGE_GAP_FLAKY
    if coverage_status == STATUS_XFAIL_WITH_EXPIRY:
        return COVERAGE_GAP_XFAIL_WITH_EXPIRY

    code = str(failure_code or "")
    if "credentials" in code:
        return COVERAGE_GAP_MISSING_CREDENTIALS
    if "runner_missing" in code or "baseline" in code or "proof_artifact_missing" in code or "proof_artifacts_required" in code:
        return COVERAGE_GAP_MISSING_COVERAGE
    if "canary_missing" in code:
        return COVERAGE_GAP_MISSING_LIVE_CANARY
    if "not_safe_no_token" in code:
        return COVERAGE_GAP_NO_TOKEN_SAFETY_GATE

    matrix_support = matrix.get("support") if matrix else None
    if coverage_status == STATUS_UNSUPPORTED_GAP:
        if matrix_support is False or code.endswith("_unsupported"):
            return COVERAGE_GAP_PROVIDER_CONTRACT_UNSUPPORTED
        return COVERAGE_GAP_UNKNOWN
    if coverage_status == STATUS_BLOCKED:
        return COVERAGE_GAP_MISSING_LIVE_CANARY
    if coverage_status == STATUS_FAIL:
        return COVERAGE_GAP_UNEXPECTED_FAILURE
    return COVERAGE_GAP_UNKNOWN


def _action_coverage_policy(action_id: str) -> str:
    if action_id in {"send_message", "session_identity"}:
        return "any_mapped_scenario"
    return "all_mapped_scenarios"


def _scenario_coverage_status(*, action_id: str, results: Iterable[ScenarioResult]) -> str:
    result_list = list(results)
    statuses = [result.status for result in result_list]
    if _action_coverage_policy(action_id) == "any_mapped_scenario" and any(
        _scenario_result_proves_action(action_id, result) for result in result_list
    ):
        return STATUS_PASS
    return _worst_status(statuses)


def _scenario_result_proves_action(action_id: str, result: ScenarioResult) -> bool:
    if result.status != STATUS_PASS:
        return False
    data = result.data if isinstance(result.data, Mapping) else {}
    if action_id == "send_message":
        return _operation_status(data, "send_input") == STATUS_PASS
    if action_id == "session_identity":
        return bool(_clean_optional_str(data.get("provider_session_id")))
    return True


def _operation_status(data: Mapping[str, Any], operation: str) -> str | None:
    evidence = data.get("operation_evidence")
    if not isinstance(evidence, Mapping):
        return None
    row = evidence.get(operation)
    if not isinstance(row, Mapping):
        return None
    status = row.get("status")
    return str(status) if status is not None else None


def _worst_status(statuses: Iterable[str]) -> str:
    seen = list(statuses)
    if any(status == STATUS_FAIL for status in seen):
        return STATUS_FAIL
    if any(status == STATUS_BLOCKED for status in seen):
        return STATUS_BLOCKED
    if any(status == STATUS_UNSUPPORTED_GAP for status in seen):
        return STATUS_UNSUPPORTED_GAP
    if any(status == STATUS_FLAKY for status in seen):
        return STATUS_FLAKY
    if any(status == STATUS_XFAIL_WITH_EXPIRY for status in seen):
        return STATUS_XFAIL_WITH_EXPIRY
    if any(status == STATUS_NOT_APPLICABLE for status in seen):
        return STATUS_NOT_APPLICABLE
    return STATUS_PASS


def _first_failure_code(results: Iterable[ScenarioResult]) -> str | None:
    for result in results:
        if result.failure_code:
            return result.failure_code
    return None


def default_db_ingest_rows() -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "role": "user",
            "text": "universal db ingest hello",
        },
        {
            "type": "assistant",
            "role": "assistant",
            "tool_name": "Bash",
            "tool_input_json": {"command": "printf universal-db-ingest"},
            "tool_call_id": "toolu_universal_db_ingest",
        },
        {
            "type": "tool",
            "role": "tool",
            "text": "universal-db-ingest",
            "tool_name": "Bash",
            "tool_output_text": "universal-db-ingest",
            "tool_call_id": "toolu_universal_db_ingest",
        },
        {
            "type": "assistant",
            "role": "assistant",
            "text": "universal db ingest done",
        },
    ]


def default_projection_rows() -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "role": "user",
            "text": "universal projection hello",
        },
        {
            "type": "assistant",
            "role": "assistant",
            "text": "universal projection reply",
        },
        {
            "type": "system",
            "role": "system",
            "text": "universal projection runtime idle",
        },
    ]


def ingest_canonical_events_into_longhouse_db(
    *,
    package: EvidencePackage,
    provider: str,
    rows: list[dict[str, Any]],
    provider_session_id: str | None = None,
) -> dict[str, Any]:
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

    from zerg.database import initialize_database
    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.agents import AgentsStore
    from zerg.services.agents import EventIngest
    from zerg.services.agents import SessionIngest
    from zerg.services.agents import SourceLineIngest
    from zerg.services.timeline_session_listing import TimelineSessionListParams
    from zerg.services.timeline_session_listing import list_timeline_sessions_for_browser

    package.write_text(
        "events/provider-raw-events.jsonl",
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
    )
    canonical = [canonical_event_from_fixture(row, provider=provider, index=index) for index, row in enumerate(rows)]
    package.write_text(
        "events/canonical-longhouse-events.jsonl",
        "\n".join(json.dumps(row, sort_keys=True) for row in canonical) + "\n",
    )

    db_path = package.path("longhouse", "db-ingest.sqlite")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    session_factory = make_sessionmaker(engine)
    session_id = uuid5(NAMESPACE_URL, f"longhouse-universal-db-ingest:{provider}:{package.root}")
    resolved_provider_session_id = provider_session_id or f"universal-db-ingest-{provider}"
    source_path = str(package.path("events", "provider-raw-events.jsonl"))
    started_at = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    event_ingests: list[Any] = []
    source_lines: list[Any] = []
    for index, row in enumerate(rows):
        raw_json = json.dumps(row, sort_keys=True)
        event_ingests.append(
            EventIngest(
                role=_event_role(row),
                content_text=_event_content_text(row),
                tool_name=_clean_optional_str(row.get("tool_name")),
                tool_input_json=row.get("tool_input_json") if isinstance(row.get("tool_input_json"), dict) else None,
                tool_output_text=_clean_optional_str(row.get("tool_output_text")),
                tool_call_id=_clean_optional_str(row.get("tool_call_id")),
                timestamp=started_at + timedelta(seconds=index),
                source_path=source_path,
                source_offset=index,
                raw_json=raw_json,
            )
        )
        source_lines.append(SourceLineIngest(source_path=source_path, source_offset=index, raw_json=raw_json))
    expected_counts = _expected_session_counts(event_ingests)
    expected_export_marker = next((event.content_text for event in event_ingests if event.content_text), None)
    expected_query_marker = _query_marker_for_events(event_ingests)

    with session_factory() as db:
        store = AgentsStore(db)
        ingest_result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider=provider,
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=started_at,
                provider_session_id=resolved_provider_session_id,
                events=event_ingests,
                source_lines=source_lines,
            )
        )
        db.commit()
        session = store.get_session(session_id)
        visible_events = store.get_session_events(session_id, limit=50)
        export_result = store.export_session_jsonl(session_id)
        query_sessions, query_total = store.list_sessions(
            include_test=True,
            project="universal-agent-harness",
            provider=provider,
            query=expected_query_marker,
            limit=10,
            hide_autonomous=False,
        )
        timeline_result = asyncio.run(
            list_timeline_sessions_for_browser(
                db=db,
                params=TimelineSessionListParams(
                    project="universal-agent-harness",
                    provider=provider,
                    environment=None,
                    include_test=True,
                    hide_autonomous=False,
                    device_id=None,
                    days_back=30,
                    query=None,
                    limit=10,
                    offset=0,
                    sort=None,
                    mode="lexical",
                    context_mode="forensic",
                ),
            )
        )

    if session is None or export_result is None:
        payload = {
            "status": STATUS_FAIL,
            "failure_code": "db_ingest_session_missing",
            "message": "Ingest completed but session or export was missing from Longhouse DB reads.",
        }
        package.write_json("assertions/db_ingest_project.json", payload)
        return payload

    timeline_cards = getattr(timeline_result.response, "sessions", [])
    timeline_card = next((card for card in timeline_cards if card.head.id == str(session_id)), None)
    timeline_preview_text = None
    if timeline_card and timeline_card.head.transcript_preview:
        timeline_preview_text = timeline_card.head.transcript_preview.text
    db_snapshot = {
        "session_id": str(session_id),
        "db_path": str(db_path),
        "ingest_result": ingest_result.model_dump(mode="json"),
        "session_counts": {
            "user_messages": int(session.user_messages or 0),
            "assistant_messages": int(session.assistant_messages or 0),
            "tool_calls": int(session.tool_calls or 0),
            "transcript_revision": int(session.transcript_revision or 0),
        },
        "visible_events": [
            {
                "role": event.role,
                "content_text": event.content_text,
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
            }
            for event in visible_events
        ],
        "query_total": query_total,
        "query_marker": expected_query_marker,
        "query_session_ids": [str(item.id) for item in query_sessions],
        "export_jsonl": export_result[0].decode("utf-8"),
        "timeline": {
            "compatibility_raw": bool(timeline_result.compatibility_raw),
            "total": int(timeline_result.response.total),
            "matched": timeline_card is not None,
            "preview_text": timeline_preview_text,
            "head_id": timeline_card.head.id if timeline_card else None,
        },
    }
    db_snapshot_path = package.write_json("longhouse/db-ingest-result.json", db_snapshot)
    session_projection = {
        **project_session(canonical, provider=provider),
        "provider_session_id": resolved_provider_session_id,
        "longhouse_session_id": str(session_id),
        "db_ingest_result_path": str(db_snapshot_path),
    }
    timeline_projection = {
        **project_timeline(canonical),
        "db_timeline_matched": timeline_card is not None,
        "db_timeline_total": int(timeline_result.response.total),
    }
    package.write_json("longhouse/session-projection.json", session_projection)
    package.write_json("longhouse/timeline-projection.json", timeline_projection)

    assertions = {
        "events_inserted": ingest_result.events_inserted == len(rows),
        "visible_event_count": len(visible_events) == len(rows),
        "export_contains_raw": expected_export_marker is None or expected_export_marker in db_snapshot["export_jsonl"],
        "query_found_session": str(session_id) in db_snapshot["query_session_ids"],
        "timeline_found_session": timeline_card is not None,
        "counts_match": _counts_match(db_snapshot["session_counts"], expected_counts),
    }
    status = STATUS_PASS if all(assertions.values()) else STATUS_FAIL
    payload = {
        "status": status,
        "failure_code": None if status == STATUS_PASS else "db_ingest_assertion_failed",
        "session_id": str(session_id),
        "raw_event_count": len(rows),
        "canonical_event_count": len(canonical),
        "db_path": str(db_path),
        "db_snapshot_path": str(db_snapshot_path),
        "session_projection_path": str(package.path("longhouse", "session-projection.json")),
        "timeline_projection_path": str(package.path("longhouse", "timeline-projection.json")),
        "assertions": assertions,
        "operation_evidence": {
            "db_ingest": {
                "status": status,
                "level": "hermetic",
                "canary": "universal_db_ingest_project",
                "failure_code": None if status == STATUS_PASS else "db_ingest_assertion_failed",
            },
            "session_projection": {
                "status": status,
                "level": "hermetic",
                "canary": "universal_db_ingest_project",
            },
            "timeline_projection": {
                "status": status,
                "level": "hermetic",
                "canary": "universal_db_ingest_project",
            },
        },
    }
    package.write_json("assertions/db_ingest_project.json", payload)
    return payload


def opencode_lineage_projection(package: EvidencePackage) -> dict[str, Any]:
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

    from zerg.database import initialize_database
    from zerg.database import make_engine
    from zerg.database import make_sessionmaker
    from zerg.models.agents import AgentEvent
    from zerg.models.agents import AgentSession
    from zerg.models.agents import SessionEdge
    from zerg.models.agents import SessionThread
    from zerg.models.agents import SessionThreadAlias
    from zerg.services.agents import AgentsStore
    from zerg.services.agents import EventIngest
    from zerg.services.agents import SessionIngest

    db_path = package.path("longhouse", "opencode-lineage.sqlite")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    session_factory = make_sessionmaker(engine)
    started_at = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    parent_id = uuid5(NAMESPACE_URL, f"{package.root}:opencode-parent")
    child_id = uuid5(NAMESPACE_URL, f"{package.root}:opencode-child")
    orphan_id = uuid5(NAMESPACE_URL, f"{package.root}:opencode-orphan-child")
    late_parent_id = uuid5(NAMESPACE_URL, f"{package.root}:opencode-late-parent")
    fork_id = uuid5(NAMESPACE_URL, f"{package.root}:opencode-fork")

    def event(text: str, session: str, offset: int) -> EventIngest:
        return EventIngest(
            role="user",
            content_text=text,
            timestamp=started_at + timedelta(seconds=offset),
            source_path=f"{package.path('raw', 'opencode.db')}#opencode:{session}",
            source_offset=offset,
            raw_json=json.dumps({"provider": "opencode", "session_id": session, "text": text}, sort_keys=True),
        )

    with session_factory() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=parent_id,
                provider="opencode",
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=started_at,
                provider_session_id="ses_parent",
                events=[event("opencode parent work", "ses_parent", 0)],
            )
        )
        child_result = store.ingest_session(
            SessionIngest(
                id=child_id,
                provider="opencode",
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=started_at,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="explore",
                subagent_tool_use_id="call_task",
                attribution_agent="explore",
                events=[event("opencode child work", "ses_child", 1)],
            )
        )
        store.ingest_session(
            SessionIngest(
                id=orphan_id,
                provider="opencode",
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=started_at,
                provider_session_id="ses_orphan_child",
                is_sidechain=True,
                parent_provider_session_id="ses_late_parent",
                subagent_id="scout",
                attribution_agent="scout",
                events=[event("opencode orphan child work", "ses_orphan_child", 2)],
            )
        )
        late_parent_result = store.ingest_session(
            SessionIngest(
                id=late_parent_id,
                provider="opencode",
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=started_at,
                provider_session_id="ses_late_parent",
                events=[event("opencode late parent work", "ses_late_parent", 3)],
            )
        )
        store.ingest_session(
            SessionIngest(
                id=fork_id,
                provider="opencode",
                environment="test",
                project="universal-agent-harness",
                device_id="universal-harness",
                cwd=str(package.path("workspace")),
                started_at=started_at,
                provider_session_id="ses_fork",
                parent_provider_session_id="ses_parent",
                lineage_kind="fork",
                events=[event("opencode fork work", "ses_fork", 4)],
            )
        )
        db.commit()

        sessions = db.query(AgentSession).order_by(AgentSession.started_at.asc(), AgentSession.id.asc()).all()
        threads = db.query(SessionThread).order_by(SessionThread.created_at.asc(), SessionThread.id.asc()).all()
        aliases = db.query(SessionThreadAlias).all()
        edges = db.query(SessionEdge).order_by(SessionEdge.edge_kind.asc(), SessionEdge.id.asc()).all()
        alias_values = {(row.thread_id, row.alias_kind, row.alias_value) for row in aliases}
        child_thread = db.query(SessionThread).filter(SessionThread.session_id == parent_id, SessionThread.branch_kind == "subagent").one()
        late_child_thread = (
            db.query(SessionThread).filter(SessionThread.session_id == late_parent_id, SessionThread.branch_kind == "subagent").one()
        )
        fork_thread = db.query(SessionThread).filter(SessionThread.session_id == fork_id, SessionThread.branch_kind == "fork").one()
        child_event = db.query(AgentEvent).filter(AgentEvent.content_text == "opencode child work").one()
        orphan_event = db.query(AgentEvent).filter(AgentEvent.content_text == "opencode orphan child work").one()
        visible_total, visible_rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)

    snapshot = {
        "db_path": str(db_path),
        "session_ids": [str(session.id) for session in sessions],
        "thread_rows": [
            {
                "id": str(thread.id),
                "session_id": str(thread.session_id),
                "provider": thread.provider,
                "branch_kind": thread.branch_kind,
                "is_primary": bool(thread.is_primary),
                "parent_thread_id": str(thread.parent_thread_id) if thread.parent_thread_id else None,
            }
            for thread in threads
        ],
        "alias_rows": [
            {
                "thread_id": str(row.thread_id),
                "alias_kind": row.alias_kind,
                "alias_value": row.alias_value,
            }
            for row in aliases
        ],
        "edge_rows": [
            {
                "id": str(row.id),
                "edge_kind": row.edge_kind,
                "visibility": row.visibility,
                "source_thread_id": str(row.source_thread_id) if row.source_thread_id else None,
                "target_thread_id": str(row.target_thread_id) if row.target_thread_id else None,
                "provider_edge_id": row.provider_edge_id,
                "metadata_json": row.metadata_json,
            }
            for row in edges
        ],
        "child_result_session_id": str(child_result.session_id),
        "late_parent_result_session_id": str(late_parent_result.session_id),
        "visible_timeline_total": visible_total,
        "visible_timeline_session_ids": [row[1] for row in visible_rows],
    }
    snapshot_path = package.write_json("longhouse/opencode-lineage-projection.json", snapshot)
    assertions = {
        "resolved_child_attached_to_parent": child_result.session_id == parent_id
        and child_event.session_id == parent_id
        and child_event.thread_id == child_thread.id,
        "child_aliases_preserved": (child_thread.id, "subagent_id", "explore") in alias_values
        and (child_thread.id, "subagent_tool_use_id", "call_task") in alias_values
        and (child_thread.id, "forked_from_provider_session_id", "ses_parent") in alias_values,
        "orphan_child_relinked_when_parent_arrived": orphan_event.session_id == late_parent_id
        and orphan_event.thread_id == late_child_thread.id
        and db_path.exists(),
        "fork_stayed_visible": fork_thread.is_primary == 1
        and (fork_thread.id, "forked_from_provider_session_id", "ses_parent") in alias_values
        and str(fork_id) in snapshot["visible_timeline_session_ids"],
        "subagent_children_not_timeline_rows": str(child_id) not in snapshot["session_ids"]
        and str(orphan_id) not in snapshot["session_ids"],
        "lineage_edges_recorded": [row["edge_kind"] for row in snapshot["edge_rows"]].count("task_child") == 2
        and "fork" in {row["edge_kind"] for row in snapshot["edge_rows"]},
    }
    status = STATUS_PASS if all(assertions.values()) else STATUS_FAIL
    payload = {
        "status": status,
        "scenario": "opencode_lineage_projection",
        "provider": "opencode",
        "projection_path": str(snapshot_path),
        "assertions": assertions,
        "operation_evidence": {
            "opencode_lineage_projection": {
                "status": status,
                "level": "hermetic",
                "canary": "universal_opencode_lineage_projection",
                "failure_code": None if status == STATUS_PASS else "opencode_lineage_projection_failed",
            },
            "db_ingest": {
                "status": status,
                "level": "hermetic",
                "canary": "universal_opencode_lineage_projection",
                "failure_code": None if status == STATUS_PASS else "opencode_lineage_projection_failed",
            },
        },
    }
    if status != STATUS_PASS:
        payload["failure_code"] = "opencode_lineage_projection_failed"
        payload["message"] = "OpenCode lineage projection assertions failed."
    package.write_json("assertions/opencode_lineage_projection.json", payload)
    return payload


def orchestration_capability_matrix(package: EvidencePackage, provider: str) -> dict[str, Any]:
    table = provider_orchestration_capabilities(provider)
    if not table:
        payload = {
            "status": STATUS_FAIL,
            "scenario": "orchestration_capability_matrix",
            "provider": provider,
            "failure_code": "orchestration_capability_matrix_missing_provider",
            "message": f"No orchestration capability table exists for provider {provider}.",
        }
        package.write_json("assertions/orchestration_capability_matrix.json", payload)
        return payload

    verdict_by_state = {
        "supported": "green",
        "observed_only": "yellow",
        "experimental": "yellow",
        "unknown": "yellow",
        "unsupported": "red",
    }
    status_by_state = {
        "supported": STATUS_PASS,
        "observed_only": STATUS_PASS,
        "experimental": STATUS_PASS,
        "unknown": STATUS_BLOCKED,
        "unsupported": STATUS_UNSUPPORTED_GAP,
    }
    rows = []
    operation_evidence = {}
    for capability, entry in sorted(table.items()):
        state = str(entry.get("state") or "unknown")
        verdict = verdict_by_state.get(state, "yellow")
        status = status_by_state.get(state, STATUS_BLOCKED)
        key = f"orchestration_{capability}"
        rows.append(
            {
                "capability": capability,
                "state": state,
                "verdict": verdict,
                "source": entry.get("source"),
            }
        )
        operation_evidence[key] = {
            "status": status,
            "level": "manifest",
            "canary": "provider_orchestration_capabilities",
            "failure_code": None if status == STATUS_PASS else f"{capability}_{state}",
            "capability_state": state,
            "verdict": verdict,
        }

    summary = {
        "green": sum(1 for row in rows if row["verdict"] == "green"),
        "yellow": sum(1 for row in rows if row["verdict"] == "yellow"),
        "red": sum(1 for row in rows if row["verdict"] == "red"),
    }
    payload = {
        "status": STATUS_PASS,
        "scenario": "orchestration_capability_matrix",
        "provider": provider,
        "summary": summary,
        "capabilities": rows,
        "operation_evidence": operation_evidence,
    }
    package.write_json("assertions/orchestration_capability_matrix.json", payload)
    return payload


def _event_role(row: Mapping[str, Any]) -> str:
    role = str(row.get("role") or row.get("type") or "system").strip().lower()
    return role if role in {"user", "assistant", "tool", "system"} else "system"


def _event_content_text(row: Mapping[str, Any]) -> str | None:
    if _event_role(row) == "assistant" and row.get("tool_name"):
        return None
    text = row.get("text")
    if text is None:
        text = row.get("content")
    return _clean_optional_str(text)


def _expected_session_counts(events: Iterable[Any]) -> dict[str, int]:
    counts = {"user_messages": 0, "assistant_messages": 0, "tool_calls": 0}
    for event in events:
        role = str(getattr(event, "role", "") or "")
        if role == "user":
            counts["user_messages"] += 1
        elif role == "assistant" and getattr(event, "tool_name", None):
            counts["tool_calls"] += 1
        elif role == "assistant":
            counts["assistant_messages"] += 1
    return counts


def _query_marker_for_events(events: Iterable[Any]) -> str | None:
    fallback = None
    for event in events:
        content = _clean_optional_str(getattr(event, "content_text", None))
        if content is None:
            continue
        fallback = fallback or content
        if str(getattr(event, "role", "") or "") in {"user", "assistant"}:
            return content
    return fallback


def _counts_match(actual: Mapping[str, Any], expected: Mapping[str, int]) -> bool:
    for key, value in expected.items():
        if int(actual.get(key) or 0) != value:
            return False
    return int(actual.get("transcript_revision") or 0) > 0


def _clean_optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def canonical_event_from_fixture(row: Mapping[str, Any], *, provider: str, index: int) -> dict[str, Any]:
    role = row.get("role")
    if role is None and row.get("type") in {"user", "assistant", "tool"}:
        role = row.get("type")
    text = row.get("text")
    if text is None and isinstance(row.get("message"), Mapping):
        text = row["message"].get("text") or row["message"].get("content")
    return {
        "schema_version": 1,
        "provider": provider,
        "index": index,
        "type": str(row.get("type") or "unknown"),
        "role": str(role or row.get("type") or "unknown"),
        "text": str(text or ""),
        "provider_event": dict(row),
    }


def project_session(events: Iterable[Mapping[str, Any]], *, provider: str) -> dict[str, Any]:
    rows = list(events)
    roles = [str(row.get("role") or "unknown") for row in rows]
    return {
        "schema_version": 1,
        "provider": provider,
        "event_count": len(rows),
        "roles": roles,
        "has_user": "user" in roles,
        "has_assistant": "assistant" in roles,
    }


def project_timeline(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(events)
    preview_text = next((str(row.get("text")) for row in rows if str(row.get("text") or "").strip()), None)
    return {
        "schema_version": 1,
        "event_count": len(rows),
        "preview_text": preview_text,
        "items": [
            {
                "index": row.get("index"),
                "type": row.get("type"),
                "role": row.get("role"),
                "text": row.get("text"),
            }
            for row in rows
        ],
    }


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


def claude_provider_live_raw_events(artifact: Mapping[str, Any], *, provider_session_id: str) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    rows: list[dict[str, Any]] = []
    binary_identity = dict(canaries.get("binary_identity") or {})
    command_shape = dict(canaries.get("command_shape") or {})
    channels_shape = dict(canaries.get("channels_shape") or {})
    detached_pty = dict(canaries.get("detached_pty_shape") or {})
    provider_version = artifact.get("provider_version") or binary_identity.get("version")
    if binary_identity:
        rows.append(
            {
                "type": "session_start",
                "role": "system",
                "text": f"Claude binary identity captured: {provider_version}",
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
                "text": "Claude launch/session command contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "command_shape",
                "status": command_shape.get("status"),
                "missing": command_shape.get("missing"),
                "failure_code": command_shape.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if channels_shape:
        rows.append(
            {
                "type": "external_event_channel",
                "role": "system",
                "text": "Claude development channel contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "channels_shape",
                "status": channels_shape.get("status"),
                "missing": channels_shape.get("missing"),
                "reason": channels_shape.get("reason"),
                "failure_code": channels_shape.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if detached_pty:
        rows.append(
            {
                "type": "runtime_phase",
                "role": "system",
                "text": "Claude detached PTY wrapper contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "detached_pty_shape",
                "status": detached_pty.get("status"),
                "platform": detached_pty.get("platform"),
                "script_path": detached_pty.get("script_path"),
                "failure_code": detached_pty.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    return rows


def _op_entry(status: str, *, level: str, canary: str, failure_code: str | None) -> dict[str, Any]:
    """One operation-evidence cell: pass/fail status, evidence level, source canary."""
    return {
        "status": status,
        "level": level if status == STATUS_PASS else "none",
        "canary": canary,
        "failure_code": failure_code,
    }


def _seed_operation_evidence(source: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Copy a canary/artifact ``operation_evidence`` blob into plain mutable dicts."""
    return {str(operation): dict(evidence) for operation, evidence in dict(source or {}).items() if isinstance(evidence, Mapping)}


def _uniform_operation_evidence(
    *,
    passed: bool,
    level: str,
    canary: str,
    default_failure_code: str,
    operations: tuple[str, ...],
    raw_failure_code: Any = None,
    seed: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build operation evidence where several operations share one status/level/canary.

    ``seed`` optionally pre-populates from an upstream ``operation_evidence`` blob
    before the uniform operations are layered on top.
    """
    status = STATUS_PASS if passed else STATUS_FAIL
    failure_code = None if passed else str(raw_failure_code or default_failure_code)
    evidence = _seed_operation_evidence(seed)
    for operation in operations:
        evidence[operation] = _op_entry(status, level=level, canary=canary, failure_code=failure_code)
    return evidence


def claude_provider_live_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    operation_evidence = {
        str(operation): dict(evidence)
        for operation, evidence in dict(artifact.get("operation_evidence") or {}).items()
        if isinstance(evidence, Mapping)
    }
    canaries = dict(artifact.get("canaries") or {})
    channels_shape = dict(canaries.get("channels_shape") or {})
    pty_shape = dict(canaries.get("detached_pty_shape") or {})
    channel_status = STATUS_PASS if channels_shape.get("status") == "pass" else STATUS_FAIL
    pty_status = STATUS_PASS if pty_shape.get("status") == "pass" else STATUS_FAIL
    if channels_shape.get("status") == "warn":
        channel_status = STATUS_BLOCKED
    operation_evidence.setdefault(
        "external_event_channel",
        {
            "status": channel_status,
            "level": "live_no_token" if channel_status == STATUS_PASS else "none",
            "canary": "claude_development_channels_contract",
            "failure_code": channels_shape.get("failure_code") or channels_shape.get("reason"),
        },
    )
    operation_evidence.setdefault(
        "runtime_phase",
        {
            "status": pty_status,
            "level": "live_no_token" if pty_status == STATUS_PASS else "none",
            "canary": "claude_detached_pty_shape",
            "failure_code": pty_shape.get("failure_code"),
        },
    )
    for operation in ("send_input", "steer_active_turn"):
        operation_evidence.setdefault(
            operation,
            {
                "status": STATUS_BLOCKED,
                "level": "live_token_required",
                "canary": "claude_live_token_contract",
                "failure_code": "claude_live_token_contract_not_run",
                "next": "Run the explicit Claude live-token provider-live contract before gating live send/steer.",
            },
        )
    return operation_evidence


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


def codex_provider_release_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    managed_tui = dict(canaries.get("managed_tui_attach") or {})
    detached_ui = dict(canaries.get("detached_ui") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-managed-session-e2e"
    rows: list[dict[str, Any]] = []
    if managed_tui:
        rows.append(
            {
                "type": "session_start",
                "role": "system",
                "text": "Codex managed TUI bridge attached to a provider thread.",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_tui_attach",
                "thread_id": managed_tui.get("thread_id"),
                "status": managed_tui.get("status"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    if detached_ui:
        rows.append(
            {
                "type": "session_reattach",
                "role": "system",
                "text": "Codex detached UI bridge exposed a resumable provider thread and IPC socket.",
                "provider_session_id": provider_session_id,
                "source_canary": "detached_ui",
                "thread_id": detached_ui.get("thread_id"),
                "status": detached_ui.get("status"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    if not rows:
        rows.append(
            {
                "type": "system",
                "role": "system",
                "text": "Codex managed-session e2e canary produced no runnable managed bridge rows.",
                "provider_session_id": provider_session_id,
                "source_canary": "codex_provider_release_canary",
                "status": artifact.get("verdict"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def _first_codex_thread_id(artifact: Mapping[str, Any]) -> str | None:
    canaries = dict(artifact.get("canaries") or {})
    for name in ("managed_tui_attach", "detached_ui", "managed_live_send", "managed_live_interrupt"):
        canary = canaries.get(name)
        if isinstance(canary, Mapping):
            thread_id = _clean_optional_str(canary.get("thread_id"))
            if thread_id:
                return thread_id
    return None


def codex_interrupt_cancel_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    interrupt = dict(canaries.get("managed_live_interrupt") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-interrupt-cancel"
    rows: list[dict[str, Any]] = []
    marker = interrupt.get("marker")
    if interrupt:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": f"Codex managed live-interrupt canary turn started: {marker or 'marker-unavailable'}",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_interrupt",
                "thread_id": interrupt.get("thread_id"),
                "state_file": interrupt.get("state_file"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
        rows.append(
            {
                "type": "interrupt",
                "role": "system",
                "text": f"Codex interrupt result: {interrupt.get('last_turn_status')}",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_interrupt",
                "last_turn_status": interrupt.get("last_turn_status"),
                "status": interrupt.get("status"),
                "failure_code": interrupt.get("failure_code"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def codex_live_token_streaming_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    live_send = dict(canaries.get("managed_live_send") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-live-token-streaming"
    marker = str(live_send.get("marker") or "marker-unavailable")
    rows: list[dict[str, Any]] = []
    if live_send:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": f"Reply exactly {marker} and nothing else.",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_send",
                "thread_id": live_send.get("thread_id"),
                "state_file": live_send.get("state_file"),
                "thread_path": live_send.get("thread_path"),
                "send_summary": live_send.get("send_summary"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": marker if live_send.get("status") == STATUS_PASS else "",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_send",
                "thread_id": live_send.get("thread_id"),
                "thread_path": live_send.get("thread_path"),
                "marker_found": live_send.get("status") == STATUS_PASS,
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def codex_tool_call_result_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    tool = dict(canaries.get("codex_real_tool_result_shape") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-tool-call-result"
    command_event = tool.get("matching_command_event")
    command_event = command_event if isinstance(command_event, Mapping) else {}
    done_event = tool.get("done_text_event")
    done_event = done_event if isinstance(done_event, Mapping) else {}
    tool_call_id = str(command_event.get("id") or "codex-real-tool-result-shape")
    command = command_event.get("command") or tool.get("command")
    output = command_event.get("aggregated_output")
    if output is None and tool.get("output_exact_match"):
        output = f"{tool.get('marker', 'marker-unavailable')}\n"
    rows: list[dict[str, Any]] = []
    if tool:
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": "Codex requested a shell command through the real-tool canary.",
                "provider_session_id": provider_session_id,
                "source_canary": "codex_real_tool_result_shape",
                "tool_name": "shell",
                "tool_input_json": {"command": command},
                "tool_call_id": tool_call_id,
                "command_status": tool.get("command_status") or command_event.get("status"),
                "command_exit_code": tool.get("command_exit_code") or command_event.get("exit_code"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
        rows.append(
            {
                "type": "tool",
                "role": "tool",
                "text": str(output or ""),
                "provider_session_id": provider_session_id,
                "source_canary": "codex_real_tool_result_shape",
                "tool_name": "shell",
                "tool_output_text": str(output or ""),
                "tool_call_id": tool_call_id,
                "command_exact_match": tool.get("command_exact_match"),
                "output_exact_match": tool.get("output_exact_match"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    if done_event or tool.get("status") == "pass":
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": str(done_event.get("text") or "DONE"),
                "provider_session_id": provider_session_id,
                "source_canary": "codex_real_tool_result_shape",
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def _opencode_control_canary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    canary = dict(dict(artifact.get("canaries") or {}).get("opencode") or {})
    return canary


def _claude_control_canary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    canary = dict(dict(artifact.get("canaries") or {}).get("claude") or {})
    return canary


def _antigravity_control_canary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    canary = dict(dict(artifact.get("canaries") or {}).get("antigravity") or {})
    return canary


def _first_claude_control_session_id(canary: Mapping[str, Any]) -> str | None:
    cleaned = _clean_optional_str(canary.get("session_id"))
    if cleaned:
        return cleaned
    session_ids = canary.get("session_ids")
    if isinstance(session_ids, list):
        for session_id in session_ids:
            cleaned = _clean_optional_str(session_id)
            if cleaned:
                return cleaned
    result_event = canary.get("result_event")
    if isinstance(result_event, Mapping):
        return _clean_optional_str(result_event.get("session_id"))
    return None


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


def claude_channel_control_raw_events(canary: Mapping[str, Any]) -> list[dict[str, Any]]:
    session_id = _first_claude_control_session_id(canary) or "claude-channel-control"
    rows: list[dict[str, Any]] = []
    if canary:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": "hello from provider control canary",
                "provider_session_id": session_id,
                "source_canary": "claude_channel_control",
                "meta": canary.get("send_meta"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": "steer from provider control canary",
                "provider_session_id": session_id,
                "source_canary": "claude_channel_control",
                "intent": "steer",
                "meta": canary.get("steer_meta"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "system",
                "role": "system",
                "text": "Claude channel interrupt delivered SIGINT to the owned fake provider process.",
                "provider_session_id": session_id,
                "source_canary": "claude_channel_control",
                "interrupt_marker": canary.get("interrupt_marker"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    return rows


def claude_real_print_raw_events(canary: Mapping[str, Any]) -> list[dict[str, Any]]:
    provider_session_id = _first_claude_control_session_id(canary) or "claude-real-print"
    marker = str(canary.get("marker") or "marker-unavailable")
    prompt = f"Reply with exactly {marker} and nothing else."
    result_event = canary.get("result_event")
    result_event = result_event if isinstance(result_event, Mapping) else {}
    exact_match = bool(result_event.get("result_exact_match"))
    rows: list[dict[str, Any]] = []
    if canary:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": prompt,
                "provider_session_id": provider_session_id,
                "source_canary": "claude_real_print",
                "marker": marker,
                "prompt_sha256": canary.get("prompt_sha256"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": marker if exact_match else "",
                "provider_session_id": provider_session_id,
                "source_canary": "claude_real_print",
                "result_exact_match": exact_match,
                "session_id_present": result_event.get("session_id_present"),
                "evidence_origin": "provider_control_e2e_canary",
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


def claude_channel_control_operation_evidence(canary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return _uniform_operation_evidence(
        passed=canary.get("status") == "pass",
        level="live_no_token",
        canary="claude_channel_control",
        default_failure_code="claude_channel_control_failed",
        operations=("send_input", "steer_active_turn", "interrupt"),
        raw_failure_code=canary.get("failure_code"),
    )


def claude_real_print_operation_evidence(canary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return _uniform_operation_evidence(
        passed=canary.get("status") == "pass",
        level="live_token",
        canary="claude_real_print",
        default_failure_code="claude_real_print_failed",
        operations=("run_once", "live_token_behavior"),
        raw_failure_code=canary.get("failure_code"),
        seed=canary.get("operation_evidence"),
    )


def codex_live_token_streaming_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    live_send = dict(dict(artifact.get("canaries") or {}).get("managed_live_send") or {})
    return _uniform_operation_evidence(
        passed=live_send.get("status") == "pass",
        level="live_token",
        canary="managed_live_send",
        default_failure_code="codex_live_token_streaming_failed",
        operations=("send_input", "live_token_behavior"),
        raw_failure_code=live_send.get("failure_code"),
        seed=artifact.get("operation_evidence"),
    )


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


def codex_tool_call_result_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    tool = dict(dict(artifact.get("canaries") or {}).get("codex_real_tool_result_shape") or {})
    return _uniform_operation_evidence(
        passed=tool.get("status") == "pass",
        level="live_token",
        canary="codex_real_tool_result_shape",
        default_failure_code="codex_tool_call_result_failed",
        operations=("tool_call_result",),
        raw_failure_code=tool.get("failure_code"),
        seed=artifact.get("operation_evidence"),
    )


def _codex_canary_credentials_gap(artifact: Mapping[str, Any], canary_names: tuple[str, ...]) -> list[str]:
    canaries = dict(artifact.get("canaries") or {})
    missing: set[str] = set()
    for name in canary_names:
        canary = canaries.get(name)
        if not isinstance(canary, Mapping):
            continue
        if canary.get("failure_code") != "managed_bridge_credentials_missing":
            continue
        values = canary.get("missing")
        if isinstance(values, list):
            missing.update(str(value) for value in values if str(value))
    return sorted(missing)


def _codex_managed_bridge_credentials_gap(artifact: Mapping[str, Any]) -> list[str]:
    return _codex_canary_credentials_gap(artifact, ("managed_tui_attach", "detached_ui"))


def run_provider_control_e2e_canary(
    *,
    provider: str,
    artifact_path: Path,
    evidence_root: Path,
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    repo_root = default_repo_root()
    script = repo_root / "scripts" / "qa" / "provider-control-e2e-canary.py"
    result = subprocess.run(
        [
            os.environ.get("PYTHON", "python3"),
            str(script),
            "--repo-root",
            str(repo_root),
            "--provider",
            provider,
            "--artifact",
            str(artifact_path),
            "--evidence-root",
            str(evidence_root),
            "--json",
            *(extra_args or []),
        ],
        cwd=str(repo_root),
        env={**os.environ, **(extra_env or {})},
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if artifact_path.is_file():
        try:
            return _read_json(artifact_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return {
                "schema_version": 1,
                "provider": provider,
                "verdict": "red",
                "failure_code": "provider_control_e2e_invalid_json",
                "message": f"{type(exc).__name__}: {exc}",
                "command": command_evidence(result),
            }
    return {
        "schema_version": 1,
        "provider": provider,
        "verdict": "red",
        "failure_code": "provider_control_e2e_missing_artifact",
        "message": "provider-control-e2e exited without writing an artifact.",
        "command": command_evidence(result),
    }


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


def provider_configs() -> dict[str, AdapterConfig]:
    providers = set(managed_provider_names()) | set(SUPPORTED_PROVIDERS)
    configs: dict[str, AdapterConfig] = {}
    for provider in SUPPORTED_PROVIDERS:
        if provider not in providers:
            continue
        binary_env = PROVIDER_CLI_ENV_BY_PROVIDER.get(provider)
        if provider == "claude":
            binary_env = binary_env or "LONGHOUSE_CLAUDE_BIN"
        safe_run_prompt_once = False
        safe_managed_session_scenarios: tuple[str, ...] = ()
        real_managed_session_e2e = False
        if provider == "claude":
            real_managed_session_e2e = True
        elif provider == "codex":
            safe_run_prompt_once = True
            safe_managed_session_scenarios = SAFE_MANAGED_SESSION_SCENARIOS
            real_managed_session_e2e = True
        elif provider == "opencode":
            safe_managed_session_scenarios = SAFE_MANAGED_SESSION_SCENARIOS
            real_managed_session_e2e = True
        elif provider == "antigravity":
            real_managed_session_e2e = True
        configs[provider] = AdapterConfig(
            provider=provider,
            binary_name=PROVIDER_CLI_BINARY_BY_PROVIDER.get(provider, provider),
            binary_env=binary_env,
            safe_run_prompt_once=safe_run_prompt_once,
            safe_managed_session_scenarios=safe_managed_session_scenarios,
            real_managed_session_e2e=real_managed_session_e2e,
        )
    return configs


def adapter_registry(provider_bins: Mapping[str, Path] | None = None) -> dict[str, AgentHarnessAdapter]:
    bins = dict(provider_bins or {})
    registry: dict[str, AgentHarnessAdapter] = {}
    for provider, config in provider_configs().items():
        adapter_class = ADAPTER_CLASS_BY_PROVIDER.get(provider, UniversalProviderAdapter)
        registry[provider] = adapter_class(config, provider_bin=bins.get(provider))
    return registry


def scenario_result(
    *,
    provider: str,
    scenario: str,
    package: EvidencePackage,
    payload: Mapping[str, Any],
) -> ScenarioResult:
    status = str(payload.get("status") or STATUS_FAIL)
    if status not in STATUSES:
        status = STATUS_FAIL
    return ScenarioResult(
        provider=provider,
        scenario=scenario,
        status=status,
        evidence_root=package.root,
        message=str(payload.get("message")) if payload.get("message") else None,
        failure_code=str(payload.get("failure_code")) if payload.get("failure_code") else None,
        data={key: value for key, value in payload.items() if key not in {"status", "message", "failure_code"}},
    )


def run_probe_identity(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.probe(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="probe_identity",
        package=package,
        payload=payload,
    )


def run_adapter_conformance(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.adapter_conformance(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="adapter_conformance",
        package=package,
        payload=payload,
    )


def run_collect_raw_evidence(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.collect_evidence(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="collect_raw_evidence",
        package=package,
        payload=payload,
    )


def run_action_matrix(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.action_matrix(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="action_matrix",
        package=package,
        payload=payload,
    )


def run_control_surface(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.control_surface(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="control_surface",
        package=package,
        payload=payload,
    )


def run_full_action_suite(
    adapter: AgentHarnessAdapter,
    package: EvidencePackage,
    old_proof_path: Path | None = None,
    new_proof_path: Path | None = None,
    baseline_root: Path | None = None,
) -> ScenarioResult:
    adapter.prepare(package)
    parse_fixture = package.write_text(
        "input/parse-fixture.jsonl",
        "\n".join(json.dumps(row, sort_keys=True) for row in default_db_ingest_rows()) + "\n",
    )
    sub_evidence_root = package.path("subruns")
    child_results: list[ScenarioResult] = []
    action_matrix_result = run_scenario(
        adapter,
        "action_matrix",
        evidence_root=sub_evidence_root,
    )
    child_results.append(action_matrix_result)
    for scenario in FULL_ACTION_SUITE_SCENARIOS:
        child_results.append(
            run_scenario(
                adapter,
                scenario,
                evidence_root=sub_evidence_root,
                fixture_path=parse_fixture if scenario == "parse_ingest_project" else None,
                old_proof_path=old_proof_path,
                new_proof_path=new_proof_path,
                baseline_root=baseline_root,
            )
        )
    adapter.cleanup(package)

    matrix_actions = _action_rows_from_result(action_matrix_result)
    child_results_by_scenario = {result.scenario: result for result in child_results}
    action_coverage = _full_action_suite_coverage(
        provider=adapter.config.provider,
        matrix_actions=matrix_actions,
        results_by_scenario=child_results_by_scenario,
    )
    missing_actions = [row["action_id"] for row in action_coverage if row["coverage_status"] == "missing"]
    failed_scenarios = [result for result in child_results if result.status == STATUS_FAIL]
    yellow_scenarios = [result for result in child_results if result.status in YELLOW_STATUSES]
    failed_actions = [row for row in action_coverage if row["coverage_status"] == STATUS_FAIL]
    yellow_actions = [row for row in action_coverage if row["coverage_status"] in YELLOW_STATUSES]

    status = STATUS_PASS
    failure_code = None
    message = None
    if failed_scenarios or failed_actions or missing_actions:
        status = STATUS_FAIL
        failure_code = "full_action_suite_failed"
        message = "One or more universal action-suite scenario executions failed or an action lost coverage."
    elif yellow_scenarios or yellow_actions:
        status = STATUS_BLOCKED
        failure_code = "full_action_suite_has_explicit_gaps"
        message = "The full action suite ran, but some actions remain blocked or unsupported by provider contracts."

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "universal_agent_harness_full_action_suite",
        "provider": adapter.config.provider,
        "generated_at": utc_now(),
        "scenario_ids": ["action_matrix", *FULL_ACTION_SUITE_SCENARIOS],
        "action_ids": list(ACTIONS),
        "missing_actions": missing_actions,
        "scenario_status_counts": _status_counts(result.status for result in child_results),
        "action_coverage_status_counts": _status_counts(
            row["coverage_status"] for row in action_coverage if row["coverage_status"] in STATUSES
        ),
        "actions": action_coverage,
        "results": [result.to_json() for result in child_results],
    }
    artifact_path = package.write_json("assertions/full-action-suite.json", artifact)
    operation_evidence = {
        "full_action_suite": {
            "status": status,
            "level": "portable_no_token",
            "canary": "universal_full_action_suite",
            "failure_code": failure_code,
        }
    }
    payload = {
        "status": status,
        "scenario": "full_action_suite",
        "failure_code": failure_code,
        "message": message,
        "full_action_suite_path": str(artifact_path),
        "scenario_count": len(child_results),
        "action_count": len(action_coverage),
        "scenario_ids": artifact["scenario_ids"],
        "action_ids": artifact["action_ids"],
        "missing_actions": missing_actions,
        "scenario_status_counts": artifact["scenario_status_counts"],
        "action_coverage_status_counts": artifact["action_coverage_status_counts"],
        "actions": action_coverage,
        "operation_evidence": operation_evidence,
    }
    if failure_code is None:
        payload.pop("failure_code")
    if message is None:
        payload.pop("message")
    package.write_json("assertions/full_action_suite.json", payload)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="full_action_suite",
        package=package,
        payload=payload,
    )


def run_parse_ingest_project(
    adapter: AgentHarnessAdapter,
    package: EvidencePackage,
    fixture_path: Path | None,
) -> ScenarioResult:
    adapter.prepare(package)
    if fixture_path is None:
        payload = {
            "status": STATUS_BLOCKED,
            "failure_code": "fixture_required",
            "message": "parse_ingest_project requires --fixture-path.",
        }
        package.write_json("assertions/parse_ingest_project.json", payload)
    else:
        payload = adapter.decode_normalize(package, fixture_path)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="parse_ingest_project",
        package=package,
        payload=payload,
    )


def run_db_ingest_project(
    adapter: AgentHarnessAdapter,
    package: EvidencePackage,
    fixture_path: Path | None,
) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.db_ingest_project(package, fixture_path)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="db_ingest_project",
        package=package,
        payload=payload,
    )


def run_opencode_lineage_projection(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    if adapter.config.provider != "opencode":
        payload = {
            "status": STATUS_NOT_APPLICABLE,
            "scenario": "opencode_lineage_projection",
            "failure_code": "opencode_lineage_projection_provider_not_applicable",
            "message": "OpenCode lineage projection only applies to the OpenCode provider.",
            "operation_evidence": {
                "opencode_lineage_projection": {
                    "status": STATUS_NOT_APPLICABLE,
                    "level": "none",
                    "canary": "universal_opencode_lineage_projection",
                    "failure_code": "opencode_lineage_projection_provider_not_applicable",
                }
            },
        }
        package.write_json("assertions/opencode_lineage_projection.json", payload)
        adapter.cleanup(package)
        return scenario_result(
            provider=adapter.config.provider,
            scenario="opencode_lineage_projection",
            package=package,
            payload=payload,
        )

    payload = opencode_lineage_projection(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="opencode_lineage_projection",
        package=package,
        payload=payload,
    )


def run_orchestration_capability_matrix(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = orchestration_capability_matrix(package, adapter.config.provider)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="orchestration_capability_matrix",
        package=package,
        payload=payload,
    )


def run_session_projection(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.session_projection(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="session_projection",
        package=package,
        payload=payload,
    )


def run_timeline_projection(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.timeline_projection(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="timeline_projection",
        package=package,
        payload=payload,
    )


def run_prompt_once(adapter: AgentHarnessAdapter, package: EvidencePackage, prompt: str | None) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.run_prompt(package, prompt or "Reply with exactly: LONGHOUSE UNIVERSAL HARNESS")
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="run_prompt_once",
        package=package,
        payload=payload,
    )


def run_launch_managed_session(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.launch_managed_session(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="launch_managed_session",
        package=package,
        payload=payload,
    )


def run_launch_remote_projection(
    adapter: AgentHarnessAdapter,
    package: EvidencePackage,
) -> ScenarioResult:
    adapter.prepare(package)
    contract = contract_for_provider(adapter.config.provider)
    if contract is None or not contract.launch_remote:
        payload = {
            "status": STATUS_UNSUPPORTED_GAP,
            "scenario": "launch_remote_projection",
            "failure_code": "launch_remote_unsupported",
            "message": f"{adapter.config.provider} does not advertise remote launch support.",
            "operation_evidence": {
                "launch_remote": {
                    "status": STATUS_UNSUPPORTED_GAP,
                    "level": "none",
                    "canary": "universal_launch_remote_projection",
                    "failure_code": "launch_remote_unsupported",
                }
            },
        }
        package.write_json("assertions/launch_remote_projection.json", payload)
        adapter.cleanup(package)
        return scenario_result(
            provider=adapter.config.provider,
            scenario="launch_remote_projection",
            package=package,
            payload=payload,
        )

    os.environ.setdefault("TESTING", "1")
    from zerg.services.session_launch_lifecycle import project_remote_launch_lifecycle

    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    attempts = {
        "dispatched": SimpleNamespace(
            state="dispatched",
            execution_lifetime="live_control",
            error_code=None,
            error_message=None,
            expires_at=now + timedelta(seconds=120),
            run_id=None,
        ),
        "adopted": SimpleNamespace(
            state="adopted",
            execution_lifetime="live_control",
            error_code=None,
            error_message=None,
            expires_at=None,
            run_id="run-universal-launch-remote",
        ),
        "failed": SimpleNamespace(
            state="failed",
            execution_lifetime="live_control",
            error_code="provider_launch_failed",
            error_message="provider exited before transcript binding",
            expires_at=None,
            run_id=None,
        ),
    }
    projections: dict[str, Any] = {}
    for name, attempt in attempts.items():
        projection = project_remote_launch_lifecycle(attempt)
        projections[name] = _json_safe(projection.__dict__)
    assertions = {
        "dispatched_projects_launching_unknown": projections["dispatched"]["state"] == "launching_unknown",
        "adopted_projects_live": projections["adopted"]["state"] == "live",
        "failed_preserves_error_code": projections["failed"]["error_code"] == "provider_launch_failed",
    }
    status = STATUS_PASS if all(assertions.values()) else STATUS_FAIL
    payload = {
        "status": status,
        "scenario": "launch_remote_projection",
        "provider": adapter.config.provider,
        "control_plane": contract.control_plane,
        "machine_control_supports": list(contract.machine_control_supports),
        "assertions": assertions,
        "projections": projections,
        "operation_evidence": {
            "launch_remote": {
                "status": status,
                "level": "hermetic" if status == STATUS_PASS else "none",
                "canary": "universal_launch_remote_projection",
                "failure_code": None if status == STATUS_PASS else "launch_remote_projection_failed",
            }
        },
    }
    if status != STATUS_PASS:
        payload["failure_code"] = "launch_remote_projection_failed"
        payload["message"] = "Remote-launch lifecycle projection assertions failed."
    package.write_json("longhouse/remote-launch-projection.json", payload)
    package.write_json("assertions/launch_remote_projection.json", payload)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="launch_remote_projection",
        package=package,
        payload=payload,
    )


def run_send_receive(adapter: AgentHarnessAdapter, package: EvidencePackage, prompt: str | None) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.send_receive(package, prompt or "LONGHOUSE UNIVERSAL HARNESS")
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="send_receive",
        package=package,
        payload=payload,
    )


def run_steer_active_turn(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.steer_active_turn(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="steer_active_turn",
        package=package,
        payload=payload,
    )


def run_pause_request_detect(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.pause_request_detect(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="pause_request_detect",
        package=package,
        payload=payload,
    )


def run_answer_pause_request(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.answer_pause_request(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="answer_pause_request",
        package=package,
        payload=payload,
    )


def run_interrupt_cancel(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.interrupt_cancel(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="interrupt_cancel",
        package=package,
        payload=payload,
    )


def run_tool_call_result(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.tool_call_result(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="tool_call_result",
        package=package,
        payload=payload,
    )


def run_tool_call_result_projection(
    adapter: AgentHarnessAdapter,
    package: EvidencePackage,
) -> ScenarioResult:
    adapter.prepare(package)
    rows = default_db_ingest_rows()
    provider_session_id = f"universal-tool-call-result-{adapter.config.provider}"
    db_ingest = ingest_canonical_events_into_longhouse_db(
        package=package,
        provider=adapter.config.provider,
        rows=rows,
        provider_session_id=provider_session_id,
    )
    operation_evidence = {
        str(operation): dict(evidence)
        for operation, evidence in dict(db_ingest.get("operation_evidence") or {}).items()
        if isinstance(evidence, Mapping)
    }
    db_status = str(db_ingest.get("status") or STATUS_FAIL)
    tool_failure_code = None
    if db_status != STATUS_PASS:
        tool_failure_code = db_ingest.get("failure_code") or "tool_call_result_projection_failed"
    tool_evidence = {
        "status": db_status,
        "level": "hermetic" if db_status == STATUS_PASS else "none",
        "canary": "universal_tool_call_result_projection",
        "failure_code": tool_failure_code,
    }
    operation_evidence["tool_call_result"] = tool_evidence
    operation_evidence["transcript_binding"] = {
        "status": db_status,
        "level": "hermetic" if db_status == STATUS_PASS else "none",
        "canary": "universal_tool_call_result_projection",
        "failure_code": tool_evidence["failure_code"],
    }
    session_projection_path = package.path("longhouse", "session-projection.json")
    try:
        session_projection = json.loads(session_projection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        session_projection = {}
    if isinstance(session_projection, dict):
        session_projection["operation_statuses"] = operation_evidence
        package.write_json("longhouse/session-projection.json", session_projection)

    payload = {
        "status": db_status,
        "scenario": "tool_call_result_projection",
        "provider_session_id": provider_session_id,
        "raw_event_count": len(rows),
        "synthetic": True,
        "operation_evidence": operation_evidence,
        "longhouse_ingest": {
            "status": db_status,
            "failure_code": db_ingest.get("failure_code"),
            "db_snapshot_path": db_ingest.get("db_snapshot_path"),
            "session_projection_path": db_ingest.get("session_projection_path"),
            "timeline_projection_path": db_ingest.get("timeline_projection_path"),
        },
    }
    if db_status != STATUS_PASS:
        payload["failure_code"] = db_ingest.get("failure_code") or "tool_call_result_projection_failed"
        payload["message"] = "Hermetic tool call/result projection did not pass Longhouse DB ingest assertions."
    package.write_json("assertions/tool_call_result_projection.json", payload)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="tool_call_result_projection",
        package=package,
        payload=payload,
    )


def run_resume_reattach(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.resume_reattach(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="resume_reattach",
        package=package,
        payload=payload,
    )


def run_terminate_cleanup(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.terminate_cleanup(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="terminate_cleanup",
        package=package,
        payload=payload,
    )


def run_tail_output(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.tail_output(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="tail_output",
        package=package,
        payload=payload,
    )


def run_runtime_phase(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.runtime_phase(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="runtime_phase",
        package=package,
        payload=payload,
    )


def run_transcript_binding(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.transcript_binding(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="transcript_binding",
        package=package,
        payload=payload,
    )


def run_multi_turn_continuity(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.multi_turn_continuity(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="multi_turn_continuity",
        package=package,
        payload=payload,
    )


def run_external_event_channel(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.external_event_channel(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="external_event_channel",
        package=package,
        payload=payload,
    )


def run_permission_prompt(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.permission_prompt(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="permission_prompt",
        package=package,
        payload=payload,
    )


def run_crash_timeout_cleanup(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.crash_timeout_cleanup(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="crash_timeout_cleanup",
        package=package,
        payload=payload,
    )


def run_live_token_streaming(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.live_token_streaming(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="live_token_streaming",
        package=package,
        payload=payload,
    )


def run_baseline_compare(
    adapter: AgentHarnessAdapter,
    package: EvidencePackage,
    baseline_root: Path | None,
) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.baseline_compare(package, baseline_root=baseline_root)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="baseline_compare",
        package=package,
        payload=payload,
    )


def run_old_new_release_diff(
    adapter: AgentHarnessAdapter,
    package: EvidencePackage,
    old_proof_path: Path | None,
    new_proof_path: Path | None,
    baseline_root: Path | None,
) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.old_new_release_diff(
        package,
        old_proof_path=old_proof_path,
        new_proof_path=new_proof_path,
        baseline_root=baseline_root,
    )
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="old_new_release_diff",
        package=package,
        payload=payload,
    )


def run_managed_session_e2e(adapter: AgentHarnessAdapter, package: EvidencePackage) -> ScenarioResult:
    adapter.prepare(package)
    payload = adapter.managed_session_e2e(package)
    adapter.cleanup(package)
    return scenario_result(
        provider=adapter.config.provider,
        scenario="managed_session_e2e",
        package=package,
        payload=payload,
    )


SCENARIO_RUNNERS = {
    "probe_identity": run_probe_identity,
    "adapter_conformance": run_adapter_conformance,
    "collect_raw_evidence": run_collect_raw_evidence,
    "action_matrix": run_action_matrix,
    "control_surface": run_control_surface,
    "full_action_suite": run_full_action_suite,
    "parse_ingest_project": run_parse_ingest_project,
    "db_ingest_project": run_db_ingest_project,
    "opencode_lineage_projection": run_opencode_lineage_projection,
    "orchestration_capability_matrix": run_orchestration_capability_matrix,
    "session_projection": run_session_projection,
    "timeline_projection": run_timeline_projection,
    "run_prompt_once": run_prompt_once,
    "launch_managed_session": run_launch_managed_session,
    "launch_remote_projection": run_launch_remote_projection,
    "send_receive": run_send_receive,
    "managed_session_e2e": run_managed_session_e2e,
    "steer_active_turn": run_steer_active_turn,
    "pause_request_detect": run_pause_request_detect,
    "answer_pause_request": run_answer_pause_request,
    "interrupt_cancel": run_interrupt_cancel,
    "tool_call_result_projection": run_tool_call_result_projection,
    "tool_call_result": run_tool_call_result,
    "resume_reattach": run_resume_reattach,
    "terminate_cleanup": run_terminate_cleanup,
    "tail_output": run_tail_output,
    "runtime_phase": run_runtime_phase,
    "transcript_binding": run_transcript_binding,
    "multi_turn_continuity": run_multi_turn_continuity,
    "external_event_channel": run_external_event_channel,
    "permission_prompt": run_permission_prompt,
    "crash_timeout_cleanup": run_crash_timeout_cleanup,
    "live_token_streaming": run_live_token_streaming,
    "baseline_compare": run_baseline_compare,
    "old_new_release_diff": run_old_new_release_diff,
}


def run_scenario(
    adapter: AgentHarnessAdapter,
    scenario: str,
    *,
    evidence_root: Path,
    fixture_path: Path | None = None,
    prompt: str | None = None,
    old_proof_path: Path | None = None,
    new_proof_path: Path | None = None,
    baseline_root: Path | None = None,
) -> ScenarioResult:
    if scenario not in SCENARIO_RUNNERS:
        package = EvidencePackage(root=evidence_root, provider=adapter.config.provider, scenario=scenario)
        package.initialize(adapter=adapter.config)
        payload = {
            "status": STATUS_FAIL,
            "failure_code": "unknown_scenario",
            "message": f"Unsupported scenario: {scenario}",
        }
        package.write_json("assertions/scenario.json", payload)
        return scenario_result(provider=adapter.config.provider, scenario=scenario, package=package, payload=payload)
    package = EvidencePackage(root=evidence_root, provider=adapter.config.provider, scenario=scenario)
    runner = SCENARIO_RUNNERS[scenario]
    if scenario == "parse_ingest_project":
        return runner(adapter, package, fixture_path)  # type: ignore[misc]
    if scenario == "db_ingest_project":
        return runner(adapter, package, fixture_path)  # type: ignore[misc]
    if scenario == "run_prompt_once":
        return runner(adapter, package, prompt)  # type: ignore[misc]
    if scenario == "send_receive":
        return runner(adapter, package, prompt)  # type: ignore[misc]
    if scenario == "baseline_compare":
        return runner(adapter, package, baseline_root)  # type: ignore[misc]
    if scenario == "old_new_release_diff":
        return runner(adapter, package, old_proof_path, new_proof_path, baseline_root)  # type: ignore[misc]
    if scenario == "full_action_suite":
        return runner(adapter, package, old_proof_path, new_proof_path, baseline_root)  # type: ignore[misc]
    return runner(adapter, package)  # type: ignore[misc]


def _proof_path_for_provider(
    provider: str,
    *,
    provider_paths: Mapping[str, Path] | None,
    fallback_path: Path | None,
) -> Path | None:
    if provider_paths and provider in provider_paths:
        return provider_paths[provider]
    return fallback_path


def verdict_for_results(results: Iterable[ScenarioResult]) -> str:
    statuses = [result.status for result in results]
    if any(status == STATUS_FAIL for status in statuses):
        return "red"
    if any(status in YELLOW_STATUSES for status in statuses):
        return "yellow"
    return "green"


def provider_support_matrix(
    *,
    providers: Iterable[str],
    scenarios: Iterable[str],
    results: Iterable[ScenarioResult],
) -> dict[str, Any] | None:
    action_rows_by_provider: dict[str, dict[str, dict[str, Any]]] = {}
    for result in results:
        if result.scenario != "action_matrix":
            continue
        rows = result.data.get("actions") if isinstance(result.data, Mapping) else None
        if not isinstance(rows, list):
            continue
        provider_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            action_id = str(row.get("action_id") or "")
            if action_id:
                provider_rows[action_id] = dict(row)
        if provider_rows:
            action_rows_by_provider[result.provider] = provider_rows

    if not action_rows_by_provider:
        return None

    provider_list = list(providers)
    matrix_rows: list[dict[str, Any]] = []
    missing_provider_actions: list[dict[str, str]] = []
    provider_statuses: dict[str, list[str]] = {provider: [] for provider in provider_list}
    for action in ACTION_DEFINITIONS:
        cells: dict[str, dict[str, Any]] = {}
        for provider in provider_list:
            row = action_rows_by_provider.get(provider, {}).get(action.action_id)
            if row is None:
                cells[provider] = {
                    "status": "missing",
                    "failure_code": "action_matrix_row_missing",
                }
                missing_provider_actions.append({"provider": provider, "action_id": action.action_id})
                continue
            status = str(row.get("status") or STATUS_FAIL)
            provider_statuses.setdefault(provider, []).append(status)
            cells[provider] = {
                key: row.get(key)
                for key in (
                    "status",
                    "support",
                    "support_reason",
                    "implementation_kind",
                    "required_evidence",
                    "evidence_level",
                    "proof_scope",
                    "canary",
                    "failure_code",
                    "next",
                )
                if row.get(key) is not None
            }
        cell_statuses = []
        for cell in cells.values():
            if cell.get("status") in STATUSES:
                cell_statuses.append(cell["status"])
        matrix_rows.append(
            {
                "action_id": action.action_id,
                "title": action.title,
                "category": action.category,
                "contract_operation": action.contract_operation,
                "required_evidence": action.required_evidence,
                "providers": cells,
                "status_counts": _status_counts(cell_statuses),
            }
        )

    provider_status_counts = {}
    for provider, statuses in provider_statuses.items():
        if statuses:
            provider_status_counts[provider] = _status_counts(statuses)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "universal_agent_harness_provider_support_matrix",
        "generated_at": utc_now(),
        "providers": provider_list,
        "scenarios": list(scenarios),
        "action_count": len(matrix_rows),
        "captured_provider_count": len(action_rows_by_provider),
        "missing_provider_actions": missing_provider_actions,
        "provider_status_counts": provider_status_counts,
        "actions": matrix_rows,
    }


def provider_execution_coverage_matrix(
    *,
    providers: Iterable[str],
    scenarios: Iterable[str],
    results: Iterable[ScenarioResult],
) -> dict[str, Any] | None:
    coverage_rows_by_provider: dict[str, dict[str, dict[str, Any]]] = {}
    for result in results:
        if result.scenario != "full_action_suite":
            continue
        rows = result.data.get("actions") if isinstance(result.data, Mapping) else None
        if not isinstance(rows, list):
            continue
        provider_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            action_id = str(row.get("action_id") or "")
            if action_id:
                provider_rows[action_id] = dict(row)
        if provider_rows:
            coverage_rows_by_provider[result.provider] = provider_rows

    if not coverage_rows_by_provider:
        return None

    provider_list = list(providers)
    matrix_rows: list[dict[str, Any]] = []
    missing_provider_actions: list[dict[str, str]] = []
    provider_statuses: dict[str, list[str]] = {provider: [] for provider in provider_list}
    provider_coverage_kinds: dict[str, list[str]] = {provider: [] for provider in provider_list}
    provider_gap_kinds: dict[str, list[str]] = {provider: [] for provider in provider_list}
    for action in ACTION_DEFINITIONS:
        cells: dict[str, dict[str, Any]] = {}
        for provider in provider_list:
            row = coverage_rows_by_provider.get(provider, {}).get(action.action_id)
            if row is None:
                cells[provider] = {
                    "coverage_status": "missing",
                    "coverage_gap_kind": COVERAGE_GAP_MISSING_COVERAGE,
                    "failure_code": "full_action_suite_row_missing",
                }
                missing_provider_actions.append({"provider": provider, "action_id": action.action_id})
                provider_gap_kinds.setdefault(provider, []).append(COVERAGE_GAP_MISSING_COVERAGE)
                continue

            coverage_status = str(row.get("coverage_status") or STATUS_FAIL)
            coverage_kind = str(row.get("coverage_kind") or "")
            coverage_gap_kind = str(row.get("coverage_gap_kind") or COVERAGE_GAP_UNKNOWN)
            provider_statuses.setdefault(provider, []).append(coverage_status)
            provider_coverage_kinds.setdefault(provider, []).append(coverage_kind)
            provider_gap_kinds.setdefault(provider, []).append(coverage_gap_kind)
            cells[provider] = {
                key: row.get(key)
                for key in (
                    "coverage_kind",
                    "coverage_status",
                    "coverage_gap_kind",
                    "failure_code",
                    "matrix_status",
                    "matrix_failure_code",
                    "matrix_support",
                    "matrix_support_reason",
                    "scenario_ids",
                    "scenario_statuses",
                    "scenario_failure_codes",
                    "coverage_policy",
                    "required_evidence",
                )
                if row.get(key) is not None
            }
        cell_statuses = []
        cell_coverage_kinds = []
        cell_gap_kinds = []
        for cell in cells.values():
            if cell.get("coverage_status") in STATUSES:
                cell_statuses.append(cell["coverage_status"])
            if cell.get("coverage_kind"):
                cell_coverage_kinds.append(str(cell["coverage_kind"]))
            if cell.get("coverage_gap_kind"):
                cell_gap_kinds.append(str(cell["coverage_gap_kind"]))
        matrix_rows.append(
            {
                "action_id": action.action_id,
                "title": action.title,
                "category": action.category,
                "contract_operation": action.contract_operation,
                "required_evidence": action.required_evidence,
                "providers": cells,
                "coverage_status_counts": _status_counts(cell_statuses),
                "coverage_kind_counts": _value_counts(cell_coverage_kinds),
                "coverage_gap_kind_counts": _value_counts(cell_gap_kinds),
            }
        )

    provider_status_counts = {}
    for provider, statuses in provider_statuses.items():
        if statuses:
            provider_status_counts[provider] = _status_counts(statuses)
    provider_coverage_kind_counts = {}
    for provider, coverage_kinds in provider_coverage_kinds.items():
        if coverage_kinds:
            provider_coverage_kind_counts[provider] = _value_counts(coverage_kinds)
    provider_gap_kind_counts = {}
    for provider, gap_kinds in provider_gap_kinds.items():
        if gap_kinds:
            provider_gap_kind_counts[provider] = _value_counts(gap_kinds)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "universal_agent_harness_provider_execution_coverage_matrix",
        "generated_at": utc_now(),
        "providers": provider_list,
        "scenarios": list(scenarios),
        "action_count": len(matrix_rows),
        "captured_provider_count": len(coverage_rows_by_provider),
        "missing_provider_actions": missing_provider_actions,
        "provider_coverage_status_counts": provider_status_counts,
        "provider_coverage_kind_counts": provider_coverage_kind_counts,
        "provider_coverage_gap_kind_counts": provider_gap_kind_counts,
        "actions": matrix_rows,
    }


def run_harness(options: HarnessOptions) -> dict[str, Any]:
    registry = adapter_registry(options.provider_bins)
    results: list[ScenarioResult] = []
    for provider in options.providers:
        adapter = registry.get(provider)
        if adapter is None:
            package = EvidencePackage(root=options.evidence_root, provider=provider, scenario="adapter_load")
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "unknown_provider",
                "message": f"Unsupported provider: {provider}",
            }
            package.write_json("assertions/adapter_load.json", payload)
            results.append(
                scenario_result(
                    provider=provider,
                    scenario="adapter_load",
                    package=package,
                    payload=payload,
                )
            )
            continue
        for scenario in options.scenarios:
            old_proof_path = _proof_path_for_provider(
                provider,
                provider_paths=options.old_proof_paths,
                fallback_path=options.old_proof_path,
            )
            new_proof_path = _proof_path_for_provider(
                provider,
                provider_paths=options.new_proof_paths,
                fallback_path=options.new_proof_path,
            )
            results.append(
                run_scenario(
                    adapter,
                    scenario,
                    evidence_root=options.evidence_root,
                    fixture_path=options.fixture_path,
                    prompt=options.prompt,
                    old_proof_path=old_proof_path,
                    new_proof_path=new_proof_path,
                    baseline_root=options.baseline_root,
                )
            )

    support_matrix = provider_support_matrix(providers=options.providers, scenarios=options.scenarios, results=results)
    execution_coverage_matrix = provider_execution_coverage_matrix(
        providers=options.providers,
        scenarios=options.scenarios,
        results=results,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "generated_at": utc_now(),
        "providers": list(options.providers),
        "scenarios": list(options.scenarios),
        "evidence_root": str(options.evidence_root),
        "verdict": verdict_for_results(results),
        "results": [result.to_json() for result in results],
    }
    if support_matrix is not None:
        matrix_path = options.evidence_root / "provider-support-matrix.json"
        write_json(matrix_path, support_matrix)
        payload["provider_support_matrix"] = support_matrix
        payload["provider_support_matrix_path"] = str(matrix_path)
    if execution_coverage_matrix is not None:
        matrix_path = options.evidence_root / "provider-execution-coverage-matrix.json"
        write_json(matrix_path, execution_coverage_matrix)
        payload["provider_execution_coverage_matrix"] = execution_coverage_matrix
        payload["provider_execution_coverage_matrix_path"] = str(matrix_path)
    write_json(options.evidence_root / "universal-agent-harness.json", payload)
    return payload


def parse_provider_bins(values: Iterable[str] | None, providers: tuple[str, ...]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values or ():
        if "=" in value:
            provider, raw_path = value.split("=", 1)
            result[provider.strip()] = Path(raw_path).expanduser()
        elif len(providers) == 1:
            result[providers[0]] = Path(value).expanduser()
        else:
            message = "--provider-bin PATH is only allowed with one provider; use provider=PATH for multi-provider runs"
            raise ValueError(message)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run universal agent harness MVP scenarios.")
    parser.add_argument(
        "--provider",
        action="append",
        choices=SUPPORTED_PROVIDERS,
        help="Provider to run. Repeatable; defaults to all.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        help="Scenario to run. Repeatable; defaults to probe_identity.",
    )
    parser.add_argument(
        "--provider-bin",
        action="append",
        help="Provider binary override: PATH for one provider or provider=PATH.",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        help="Evidence root. Defaults to .build/canaries/universal-agent-harness/<timestamp>.",
    )
    parser.add_argument("--fixture-path", type=Path, help="JSONL fixture for parse_ingest_project.")
    parser.add_argument("--prompt", help="Prompt for run_prompt_once.")
    parser.add_argument(
        "--old-proof-artifact",
        type=Path,
        help="Old provider release-proof artifact for old_new_release_diff.",
    )
    parser.add_argument(
        "--new-proof-artifact",
        type=Path,
        help="New provider release-proof artifact for old_new_release_diff.",
    )
    parser.add_argument("--baseline-root", type=Path, help="Baseline root passed through to old_new_release_diff.")
    parser.add_argument("--json", action="store_true", help="Emit JSON artifact to stdout.")
    return parser


def default_evidence_root() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return default_repo_root() / ".build/canaries/universal-agent-harness" / timestamp


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    providers = tuple(args.provider or SUPPORTED_PROVIDERS)
    scenarios = tuple(args.scenario or ("probe_identity",))
    try:
        provider_bins = parse_provider_bins(args.provider_bin, providers)
    except ValueError as exc:
        parser.error(str(exc))
    artifact = run_harness(
        HarnessOptions(
            providers=providers,
            scenarios=scenarios,
            evidence_root=(args.evidence_root or default_evidence_root()).expanduser(),
            provider_bins=provider_bins,
            fixture_path=args.fixture_path.expanduser() if args.fixture_path else None,
            prompt=args.prompt,
            old_proof_path=args.old_proof_artifact.expanduser() if args.old_proof_artifact else None,
            new_proof_path=args.new_proof_artifact.expanduser() if args.new_proof_artifact else None,
            baseline_root=args.baseline_root.expanduser() if args.baseline_root else None,
        )
    )
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"verdict: {artifact['verdict']}")
        print(f"artifact: {Path(artifact['evidence_root']) / 'universal-agent-harness.json'}")
    return 1 if artifact.get("verdict") == "red" else 0


__all__ = [
    "ARTIFACT_KIND",
    "ACTIONS",
    "ACTION_DEFINITIONS",
    "CONTROL_SURFACE_ACTION_IDS",
    "SCENARIOS",
    "STATUSES",
    "SUPPORTED_PROVIDERS",
    "ADAPTER_CLASS_BY_PROVIDER",
    "AntigravityHarnessAdapter",
    "AdapterConfig",
    "AgentHarnessAdapter",
    "ClaudeCodeHarnessAdapter",
    "CodexOpenAIHarnessAdapter",
    "EvidencePackage",
    "HarnessOptions",
    "OpenCodeHarnessAdapter",
    "ScenarioResult",
    "adapter_registry",
    "provider_execution_coverage_matrix",
    "provider_support_matrix",
    "provider_configs",
    "run_harness",
    "run_scenario",
]


if __name__ == "__main__":
    raise SystemExit(main())
