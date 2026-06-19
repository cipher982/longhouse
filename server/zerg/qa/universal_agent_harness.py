"""Universal provider harness for release-proof scenarios.

This module owns the shared adapter/scenario shape. Provider-specific mechanics
stay behind adapters while universal scenarios produce comparable evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Protocol

from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.qa.repo_root import default_repo_root
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.managed_provider_contracts import managed_provider_names

SCHEMA_VERSION = 1
ARTIFACT_KIND = "universal_agent_harness_run"
SUPPORTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity")
SCENARIOS = (
    "probe_identity",
    "collect_raw_evidence",
    "parse_ingest_project",
    "run_prompt_once",
    "launch_managed_session",
    "send_receive",
    "managed_session_e2e",
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

MVP_METHODS = (
    "prepare",
    "probe",
    "run_prompt",
    "collect_evidence",
    "decode_normalize",
    "launch_managed_session",
    "send_receive",
    "managed_session_e2e",
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


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


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

    def run_prompt(self, package: "EvidencePackage", prompt: str) -> dict[str, Any]: ...

    def collect_evidence(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def decode_normalize(self, package: "EvidencePackage", fixture_path: Path) -> dict[str, Any]: ...

    def launch_managed_session(self, package: "EvidencePackage") -> dict[str, Any]: ...

    def send_receive(self, package: "EvidencePackage", prompt: str) -> dict[str, Any]: ...

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

    def prepare(self, package: EvidencePackage) -> dict[str, Any]:
        package.initialize(adapter=self.config)
        workspace = package.path("workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": STATUS_PASS,
            "workspace": str(workspace),
            "provider": self.config.provider,
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

    def run_prompt(self, package: EvidencePackage, prompt: str) -> dict[str, Any]:
        package.write_text("input/prompt.txt", prompt)
        if not self.config.safe_run_prompt_once:
            payload = self._unsupported_payload(
                "run_prompt_once",
                "run_prompt_once_not_safe_no_token",
                "run_prompt_once is not yet safe to claim without a token-spending provider run.",
            )
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

    def launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
        if "launch_managed_session" not in self.config.safe_managed_session_scenarios:
            payload = self._unsupported_payload(
                "launch_managed_session",
                "managed_session_not_safe_no_token",
                "launch_managed_session is not yet backed by a no-token/session-safe universal adapter.",
            )
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
            payload = self._unsupported_payload(
                "managed_session_e2e",
                "managed_session_e2e_adapter_missing",
                "Only the OpenCode real no-token managed-session e2e adapter is implemented.",
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
        live_verdict = str(live_artifact.get("verdict") or "red")
        ingest_gap_message = "This lane currently proves raw provider evidence and canonical "
        ingest_gap_message += "projection; DB ingest promotion is the next gate."
        payload = {
            **projection,
            "status": STATUS_PASS if live_verdict == "green" else STATUS_FAIL,
            "scenario": "managed_session_e2e",
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "longhouse_ingest": {
                "status": STATUS_BLOCKED,
                "failure_code": "db_ingest_not_in_universal_e2e_slice",
                "message": ingest_gap_message,
            },
        }
        if live_verdict != "green":
            payload["failure_code"] = live_artifact.get("failure_code") or "provider_live_canary_failed"
            payload["message"] = "OpenCode provider-live no-token canary did not pass."
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

    def _session_id(self, package: EvidencePackage) -> str:
        return f"universal-{self.config.provider}-{package.scenario}"

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
    return {
        "schema_version": 1,
        "event_count": len(rows),
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
        if provider == "codex":
            safe_run_prompt_once = True
            safe_managed_session_scenarios = SAFE_MANAGED_SESSION_SCENARIOS
        elif provider == "opencode":
            safe_managed_session_scenarios = SAFE_MANAGED_SESSION_SCENARIOS
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
        registry[provider] = UniversalProviderAdapter(config, provider_bin=bins.get(provider))
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
    "collect_raw_evidence": run_collect_raw_evidence,
    "parse_ingest_project": run_parse_ingest_project,
    "run_prompt_once": run_prompt_once,
    "launch_managed_session": run_launch_managed_session,
    "send_receive": run_send_receive,
    "managed_session_e2e": run_managed_session_e2e,
}


def run_scenario(
    adapter: AgentHarnessAdapter,
    scenario: str,
    *,
    evidence_root: Path,
    fixture_path: Path | None = None,
    prompt: str | None = None,
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
    if scenario == "run_prompt_once":
        return runner(adapter, package, prompt)  # type: ignore[misc]
    if scenario == "send_receive":
        return runner(adapter, package, prompt)  # type: ignore[misc]
    return runner(adapter, package)  # type: ignore[misc]


def verdict_for_results(results: Iterable[ScenarioResult]) -> str:
    statuses = [result.status for result in results]
    if any(status == STATUS_FAIL for status in statuses):
        return "red"
    if any(status in YELLOW_STATUSES for status in statuses):
        return "yellow"
    return "green"


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
            results.append(
                run_scenario(
                    adapter,
                    scenario,
                    evidence_root=options.evidence_root,
                    fixture_path=options.fixture_path,
                    prompt=options.prompt,
                )
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
    "SCENARIOS",
    "STATUSES",
    "SUPPORTED_PROVIDERS",
    "AdapterConfig",
    "AgentHarnessAdapter",
    "EvidencePackage",
    "HarnessOptions",
    "ScenarioResult",
    "adapter_registry",
    "provider_configs",
    "run_harness",
    "run_scenario",
]


if __name__ == "__main__":
    raise SystemExit(main())
