"""Managed provider session contract files.

These contracts record launch-time local provenance for managed provider
sessions. They are deliberately separate from ``managed_session_state``:
SQLite owns current phase/workspace truth, while these JSON files own the local
execution and control contract that local-health can verify later.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.services.longhouse_paths import resolve_longhouse_home

CONTRACT_SCHEMA_VERSION = 1
DIAGNOSTIC_SCHEMA_VERSION = 1

REASON_PROVIDER_SESSION_CWD_MISSING = "provider_session_cwd_missing"
REASON_PROVIDER_SESSION_CWD_REPLACED = "provider_session_cwd_replaced"
REASON_BRIDGE_STATE_PATH_MISSING = "bridge_state_path_missing"

_SAFE_PATH_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ManagedSessionContractIssue:
    reason: str
    session_id: str | None
    provider: str | None
    severity: str
    headline: str
    action: str
    source_path: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "session_id": self.session_id,
            "provider": self.provider,
            "severity": self.severity,
            "headline": self.headline,
            "action": self.action,
            "source_path": self.source_path,
            "detail": dict(self.detail),
        }


def managed_session_contract_root(base_dir: str | Path | None = None) -> Path:
    resolved_base = Path(base_dir).expanduser() if base_dir is not None else None
    return resolve_longhouse_home(resolved_base) / "managed-local" / "contracts"


def build_managed_session_contract_path(
    *,
    provider: str,
    session_id: str,
    base_dir: str | Path | None = None,
) -> Path:
    normalized_provider = _safe_component(provider, fallback="unknown")
    normalized_session = _safe_component(session_id, fallback="unknown-session")
    return managed_session_contract_root(base_dir) / normalized_provider / f"{normalized_session}.json"


def current_path_file_identity(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        stat_result = Path(path).expanduser().stat()
    except OSError:
        return None
    return f"dev={stat_result.st_dev},ino={stat_result.st_ino}"


def build_managed_session_contract(
    *,
    session_id: str,
    provider: str,
    cwd: str | Path,
    launch_mode: str | None = None,
    provider_binary_path: str | None = None,
    provider_binary_source: str | None = None,
    provider_version: str | None = None,
    control_kind: str | None = None,
    control_state_path: str | Path | None = None,
    longhouse_build: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    cwd_path = Path(cwd).expanduser()
    timestamp = _to_rfc3339(created_at or datetime.now(timezone.utc))
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "session_id": str(session_id or "").strip(),
        "provider": str(provider or "").strip(),
        "launch_mode": _normalize_optional_string(launch_mode),
        "created_at": timestamp,
        "longhouse_build": _normalize_optional_string(longhouse_build),
        "provider_binary": {
            "path": _normalize_optional_string(provider_binary_path),
            "source": _normalize_optional_string(provider_binary_source),
            "version": _normalize_optional_string(provider_version),
        },
        "workspace": {
            "cwd": str(cwd_path),
            "canonical_cwd": str(cwd_path.resolve(strict=False)),
            "file_identity": current_path_file_identity(cwd_path),
        },
        "control": {
            "kind": _normalize_optional_string(control_kind),
            "state_path": str(Path(control_state_path).expanduser()) if control_state_path is not None else None,
        },
    }


def write_managed_session_contract(
    contract: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    session_id = _normalize_optional_string(contract.get("session_id"))
    provider = _normalize_optional_string(contract.get("provider"))
    if session_id is None:
        raise ValueError("managed session contract requires session_id")
    if provider is None:
        raise ValueError("managed session contract requires provider")

    path = build_managed_session_contract_path(provider=provider, session_id=session_id, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(dict(contract), indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return path


def list_managed_session_contracts(
    base_dir: str | Path | None = None,
    *,
    session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    root = managed_session_contract_root(base_dir)
    if not root.exists():
        return []
    contracts: list[dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*/*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            contracts.append(
                {
                    "schema_version": None,
                    "session_id": None,
                    "provider": path.parent.name,
                    "source_path": str(path),
                    "read_error": str(exc),
                }
            )
            continue
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["source_path"] = str(path)
            session_id = _normalize_optional_string(payload.get("session_id"))
            if session_ids is not None and session_id not in session_ids:
                continue
            contracts.append(payload)
    return contracts


def verify_managed_session_contracts(
    contracts: list[Mapping[str, Any]],
) -> dict[str, Any]:
    issues: list[ManagedSessionContractIssue] = []
    for contract in contracts:
        issues.extend(_issues_for_contract(contract))

    serialized_issues = [issue.to_dict() for issue in issues]
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "state": "degraded" if issues else "healthy",
        "contracts_count": len(contracts),
        "issue_count": len(issues),
        "issues": serialized_issues,
        "latest": serialized_issues[0] if serialized_issues else None,
    }


def collect_managed_session_contract_diagnostics(
    base_dir: str | Path | None = None,
    *,
    session_ids: set[str] | None = None,
) -> dict[str, Any]:
    root = managed_session_contract_root(base_dir)
    contracts = list_managed_session_contracts(base_dir, session_ids=session_ids)
    diagnostics = verify_managed_session_contracts(contracts)
    diagnostics["root"] = str(root)
    return diagnostics


def capture_provider_version(provider_binary_path: str | None, *, timeout_seconds: float = 5.0) -> str | None:
    binary = _normalize_optional_string(provider_binary_path)
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = ((completed.stdout or "").strip() or (completed.stderr or "").strip()).splitlines()
    return output[0].strip() if output and output[0].strip() else None


def _issues_for_contract(contract: Mapping[str, Any]) -> list[ManagedSessionContractIssue]:
    source_path = _normalize_optional_string(contract.get("source_path")) or ""
    if _normalize_optional_string(contract.get("read_error")) is not None:
        return []

    session_id = _normalize_optional_string(contract.get("session_id"))
    provider = _normalize_optional_string(contract.get("provider"))
    workspace = contract.get("workspace")
    control = contract.get("control")
    workspace_map = workspace if isinstance(workspace, Mapping) else {}
    control_map = control if isinstance(control, Mapping) else {}

    issues: list[ManagedSessionContractIssue] = []
    cwd = _normalize_optional_string(workspace_map.get("canonical_cwd")) or _normalize_optional_string(workspace_map.get("cwd"))
    recorded_identity = _normalize_optional_string(workspace_map.get("file_identity"))
    if cwd is not None:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.exists():
            issues.append(
                ManagedSessionContractIssue(
                    reason=REASON_PROVIDER_SESSION_CWD_MISSING,
                    session_id=session_id,
                    provider=provider,
                    severity="yellow",
                    headline="A provider session working directory disappeared",
                    action=("Restart or reattach the affected provider session from an existing directory; " f"missing cwd: {cwd}"),
                    source_path=source_path,
                    detail={"cwd": cwd},
                )
            )
        elif recorded_identity is not None:
            current_identity = current_path_file_identity(cwd_path)
            if current_identity is not None and current_identity != recorded_identity:
                issues.append(
                    ManagedSessionContractIssue(
                        reason=REASON_PROVIDER_SESSION_CWD_REPLACED,
                        session_id=session_id,
                        provider=provider,
                        severity="yellow",
                        headline="A provider session working directory was replaced",
                        action=(
                            "Restart or reattach the affected provider session after verifying the workspace "
                            f"was intentionally recreated: {cwd}"
                        ),
                        source_path=source_path,
                        detail={
                            "cwd": cwd,
                            "recorded_file_identity": recorded_identity,
                            "current_file_identity": current_identity,
                        },
                    )
                )

    state_path = _normalize_optional_string(control_map.get("state_path"))
    if state_path is not None and not Path(state_path).expanduser().exists():
        issues.append(
            ManagedSessionContractIssue(
                reason=REASON_BRIDGE_STATE_PATH_MISSING,
                session_id=session_id,
                provider=provider,
                severity="yellow",
                headline="A managed provider bridge state file is missing",
                action=f"Restart or detach the affected managed session; missing bridge state: {state_path}",
                source_path=source_path,
                detail={"state_path": state_path},
            )
        )
    return issues


def _safe_component(value: str | None, *, fallback: str) -> str:
    normalized = _normalize_optional_string(value) or fallback
    safe = _SAFE_PATH_COMPONENT_RE.sub("-", normalized).strip(".-")
    return safe or fallback


def _normalize_optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _to_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "REASON_BRIDGE_STATE_PATH_MISSING",
    "REASON_PROVIDER_SESSION_CWD_MISSING",
    "REASON_PROVIDER_SESSION_CWD_REPLACED",
    "build_managed_session_contract",
    "build_managed_session_contract_path",
    "capture_provider_version",
    "collect_managed_session_contract_diagnostics",
    "current_path_file_identity",
    "list_managed_session_contracts",
    "managed_session_contract_root",
    "verify_managed_session_contracts",
    "write_managed_session_contract",
]
