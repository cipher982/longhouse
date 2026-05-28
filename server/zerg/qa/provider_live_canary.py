#!/usr/bin/env python3
"""Live upstream managed-provider canaries.

These canaries exercise the installed upstream provider binary directly. They
are the source-drift layer above the hermetic Longhouse control E2E canaries.
The OpenCode lane intentionally avoids prompt execution so it can run without
spending model tokens.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import resolve_longhouse_home

PROVIDER_STATUS_SCHEMA_VERSION = 1
_OPENCODE_SERVER_LOG_RE = re.compile(r"opencode server listening on (?P<url>http://127\.0\.0\.1:\d+)")
_ANTIGRAVITY_PLUGIN_NAME = "longhouse-runtime"
_ANTIGRAVITY_HOOK_EVENTS = ("PreInvocation", "PreToolUse", "PostToolUse", "PostInvocation", "Stop")
_GAP_OPERATION_STATUSES = {"fail", "missing", "not_run", "skipped", "stale"}
_OPENCODE_REATTACH_MESSAGE = " ".join(
    (
        "API-surface proof: attach command exposes session and auth flags;",
        "process-restart reattach is future proof.",
    )
)
_OPENCODE_PROMPT_ASYNC_MESSAGE = " ".join(
    (
        "No-token behavior proof: prompt_async accepted a noReply input and",
        "session.messages returned the delivered marker;",
        "token-spending assistant-response proof is future work.",
    )
)
_OPENCODE_ASSISTANT_RESPONSE_MESSAGE = " ".join(
    (
        "Live-token behavior proof: OpenCode returned an assistant response",
        "and session.messages exposed the assistant text marker.",
    )
)
_OPENCODE_ACTIVE_ABORT_MESSAGE = " ".join(
    (
        "Live-token behavior proof: OpenCode accepted abort during an",
        "in-flight message turn and observed MessageAbortedError.",
    )
)
_CLAUDE_CHANNEL_UNCONFIRMED_MESSAGE = (
    "Claude recognized the development channel flag, but launch help did not confirm the session-control shape."
)
_ANTIGRAVITY_PLUGIN_NOTE = (
    "agy may report hook components as skipped here; Longhouse wires Antigravity hooks through the global hooks config."
)
_CODEX_LIGHTWEIGHT_MESSAGE = (
    "Shared provider-live proof ran the portable Codex release checks only. "
    "Bridge/TUI no-token canaries stay in the explicit Codex release lane."
)


def _source_repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        contract_path = parent / "server/zerg/config/managed_provider_contracts.json"
        if contract_path.exists() and (parent / "scripts/qa").exists():
            return parent
    return None


def default_repo_root() -> Path:
    return _source_repo_root() or Path.cwd()


def _default_evidence_root(repo_root: Path, provider: str, timestamp: str) -> Path:
    source_root = _source_repo_root()
    if source_root is not None and repo_root.resolve() == source_root.resolve():
        return repo_root / ".build/canaries/provider-live" / provider / timestamp
    return resolve_longhouse_home() / "canaries/provider-live" / provider / timestamp


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _status(status: str, **fields: Any) -> dict[str, Any]:
    payload = {"status": status}
    payload.update(fields)
    return payload


def _fail(code: str, message: str, **fields: Any) -> dict[str, Any]:
    payload = {"status": "fail", "failure_code": code, "message": message}
    payload.update(fields)
    return payload


def _command_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "argv": list(result.args) if isinstance(result.args, list) else result.args,
        "returncode": result.returncode,
        "stdout": (result.stdout or "")[-4000:],
        "stderr": (result.stderr or "")[-4000:],
    }


def _metadata_only_command_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "argv": list(result.args) if isinstance(result.args, list) else result.args,
        "returncode": result.returncode,
        "stdout_chars": len(result.stdout or ""),
        "stderr_chars": len(result.stderr or ""),
    }


def _run_version(binary: str) -> tuple[str | None, dict[str, Any]]:
    try:
        result = subprocess.run(
            [binary, "--version"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, {"argv": [binary, "--version"], "error": f"{type(exc).__name__}: {exc}"}
    evidence = _command_evidence(result)
    if result.returncode != 0:
        return None, evidence
    return (result.stdout or result.stderr).strip() or None, evidence


def _resolve_provider_binary(args: argparse.Namespace, binary_name: str) -> str | None:
    if args.provider_bin:
        path = Path(args.provider_bin).expanduser()
        return str(path) if path.is_file() else None
    return shutil.which(binary_name)


def _provider_contract(repo_root: Path, provider: str) -> dict[str, Any] | None:
    repo_path = repo_root / "server/zerg/config/managed_provider_contracts.json"
    package_path = Path(__file__).resolve().parents[1] / "config/managed_provider_contracts.json"
    path = repo_path if repo_path.exists() else package_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for item in payload.get("providers") or []:
        if isinstance(item, dict) and item.get("provider") == provider:
            return dict(item)
    return None


def _manifest_operation_evidence(contract: dict[str, Any], operation: str) -> dict[str, Any]:
    evidence = (contract.get("operation_evidence") or {}).get(operation)
    return dict(evidence) if isinstance(evidence, dict) else {}


def _operation_entry(
    contract: dict[str, Any],
    operation: str,
    *,
    status: str,
    canary: str,
    canaries: list[str] | None = None,
    level: str | None = None,
    source: str | None = None,
    message: str | None = None,
    failure_code: str | None = None,
    next_note: str | None = None,
) -> dict[str, Any]:
    target = _manifest_operation_evidence(contract, operation)
    if status in _GAP_OPERATION_STATUSES:
        effective_level = level or "none"
    else:
        effective_level = level or str(target.get("level") or "none")
    entry: dict[str, Any] = {
        "status": status,
        "level": effective_level,
        "source": source or target.get("source"),
        "canary": canary,
    }
    if canaries:
        entry["canaries"] = canaries
    if failure_code:
        entry["failure_code"] = failure_code
    if message:
        entry["message"] = message
    if next_note or target.get("next"):
        entry["next"] = next_note or target.get("next")
    return {key: value for key, value in entry.items() if value is not None}


def _group_operation_status(canaries: dict[str, dict[str, Any]], names: list[str]) -> tuple[str, dict[str, Any] | None]:
    saw_warn: dict[str, Any] | None = None
    for name in names:
        canary = canaries.get(name)
        if not isinstance(canary, dict):
            return "", None
        status = canary.get("status")
        if status == "fail":
            return "fail", canary
        if status == "warn" and saw_warn is None:
            saw_warn = canary
        elif status != "pass":
            return "", None
    if saw_warn is not None:
        return "warn", saw_warn
    return "pass", None


def _entry_from_canary_group(
    contract: dict[str, Any],
    operation: str,
    *,
    canaries: dict[str, dict[str, Any]],
    required: list[str],
    canary_name: str,
    level: str | None = None,
    source: str | None = None,
    message: str | None = None,
    next_note: str | None = None,
) -> dict[str, Any] | None:
    status, detail = _group_operation_status(canaries, required)
    if not status:
        return None
    return _operation_entry(
        contract,
        operation,
        status=status,
        canary=canary_name,
        canaries=required,
        level="none" if status == "fail" else level,
        source=source,
        message=message or (detail or {}).get("message"),
        failure_code=(detail or {}).get("failure_code"),
        next_note=next_note,
    )


def _schema_probe_failed_for(
    canaries: dict[str, dict[str, Any]],
    path: str,
    operation_id: str,
) -> dict[str, Any] | None:
    schema_probe = canaries.get("schema_probe")
    if not isinstance(schema_probe, dict) or schema_probe.get("status") != "fail":
        return None
    for failure in schema_probe.get("failures") or []:
        if not isinstance(failure, dict):
            continue
        if failure.get("path") == path or failure.get("expected") == operation_id:
            return failure
    return None


def _claude_operation_evidence(
    contract: dict[str, Any],
    canaries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    launch_local = _entry_from_canary_group(
        contract,
        "launch_local",
        canaries=canaries,
        required=["binary_identity", "command_shape", "channels_shape", "detached_pty_shape"],
        canary_name="claude_launch_local_no_token",
    )
    return {"launch_local": launch_local} if launch_local else {}


def _opencode_operation_evidence(
    contract: dict[str, Any],
    canaries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    launch_local = _entry_from_canary_group(
        contract,
        "launch_local",
        canaries=canaries,
        required=["binary_identity", "server_startup", "session_create", "session_get"],
        canary_name="opencode_server_session_no_token",
    )
    if launch_local:
        evidence["launch_local"] = launch_local

    reattach = _entry_from_canary_group(
        contract,
        "reattach",
        canaries=canaries,
        required=["binary_identity", "attach_command_shape"],
        canary_name="opencode_attach_surface",
        message=_OPENCODE_REATTACH_MESSAGE,
    )
    if reattach:
        evidence["reattach"] = reattach

    prompt_failure = _schema_probe_failed_for(canaries, "/session/{sessionID}/message", "session.prompt")
    prompt_async_failure = _schema_probe_failed_for(
        canaries,
        "/session/{sessionID}/prompt_async",
        "session.prompt_async",
    )
    if prompt_failure:
        evidence["send_input"] = _operation_entry(
            contract,
            "send_input",
            status="fail",
            canary="opencode_prompt_schema",
            level="none",
            failure_code=prompt_failure.get("failure_code") or "opencode_prompt_schema_failed",
            message=prompt_failure.get("message"),
        )
    elif prompt_async_failure:
        evidence["send_input"] = _operation_entry(
            contract,
            "send_input",
            status="fail",
            canary="opencode_prompt_async_schema",
            level="none",
            failure_code=prompt_async_failure.get("failure_code") or "opencode_prompt_async_schema_failed",
            message=prompt_async_failure.get("message"),
        )
    elif (canaries.get("assistant_response_contract") or {}).get("status") in {"pass", "fail", "warn"}:
        assistant_send = _entry_from_canary_group(
            contract,
            "send_input",
            canaries=canaries,
            required=[
                "binary_identity",
                "schema_probe",
                "prompt_async_no_reply_delivery",
                "assistant_response_contract",
            ],
            canary_name="opencode_assistant_response_contract",
            level="manual_live_token",
            source="longhouse provider-live canary --provider opencode --run-live-token-contract",
            message=_OPENCODE_ASSISTANT_RESPONSE_MESSAGE,
            next_note="promote with future live-token process-restart reattach canary",
        )
        if assistant_send:
            evidence["send_input"] = assistant_send
        transcript_binding = _entry_from_canary_group(
            contract,
            "transcript_binding",
            canaries=canaries,
            required=["assistant_response_contract"],
            canary_name="opencode_assistant_response_contract",
            level="manual_live_token",
            source="longhouse provider-live canary --provider opencode --run-live-token-contract",
            message="Live-token proof: assistant response marker was visible through session.messages.",
            next_note="promote with future live-token process-restart reattach canary",
        )
        if transcript_binding:
            evidence["transcript_binding"] = transcript_binding
    elif canaries.get("prompt_async_no_reply_delivery"):
        send_input = _entry_from_canary_group(
            contract,
            "send_input",
            canaries=canaries,
            required=["binary_identity", "schema_probe", "prompt_async_no_reply_delivery"],
            canary_name="opencode_prompt_async_no_reply_delivery",
            message=_OPENCODE_PROMPT_ASYNC_MESSAGE,
        )
        if send_input:
            evidence["send_input"] = send_input
    elif canaries.get("schema_probe", {}).get("status") == "pass":
        evidence["send_input"] = _operation_entry(
            contract,
            "send_input",
            status="pass",
            canary="opencode_prompt_async_schema",
            canaries=["schema_probe"],
            message=_OPENCODE_PROMPT_ASYNC_MESSAGE,
        )

    interrupt_failure = _schema_probe_failed_for(canaries, "/session/{sessionID}/abort", "session.abort")
    active_abort_status = (canaries.get("active_turn_abort_contract") or {}).get("status")
    if active_abort_status in {"pass", "fail", "warn"}:
        interrupt = _entry_from_canary_group(
            contract,
            "interrupt",
            canaries=canaries,
            required=["binary_identity", "schema_probe", "active_turn_abort_contract"],
            canary_name="opencode_active_turn_abort_contract",
            level="manual_live_token",
            source="longhouse provider-live canary --provider opencode --run-live-token-contract",
            message=_OPENCODE_ACTIVE_ABORT_MESSAGE,
            next_note="promote with future live-token process-restart reattach canary",
        )
        if interrupt:
            evidence["interrupt"] = interrupt
    elif interrupt_failure:
        evidence["interrupt"] = _operation_entry(
            contract,
            "interrupt",
            status="fail",
            canary="opencode_abort_endpoint",
            level="none",
            failure_code=interrupt_failure.get("failure_code") or "opencode_abort_schema_failed",
            message=interrupt_failure.get("message"),
        )
    else:
        interrupt = _entry_from_canary_group(
            contract,
            "interrupt",
            canaries=canaries,
            required=["binary_identity", "session_abort"],
            canary_name="opencode_abort_endpoint",
            message="API-surface proof: abort endpoint accepted a request against a created session.",
        )
        if interrupt:
            evidence["interrupt"] = interrupt

    return evidence


def _antigravity_operation_evidence(
    contract: dict[str, Any],
    canaries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    launch_local = _entry_from_canary_group(
        contract,
        "launch_local",
        canaries=canaries,
        required=["binary_identity", "command_shape", "plugin_contract", "global_hooks_contract"],
        canary_name="antigravity_launch_local_no_token",
    )
    return {"launch_local": launch_local} if launch_local else {}


def _provider_operation_evidence(
    repo_root: Path,
    provider: str,
    canaries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    contract = _provider_contract(repo_root, provider)
    if contract is None:
        return {}
    if provider == "claude":
        return _claude_operation_evidence(contract, canaries)
    if provider == "opencode":
        return _opencode_operation_evidence(contract, canaries)
    if provider == "antigravity":
        return _antigravity_operation_evidence(contract, canaries)
    return {}


def run_codex_live_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

    codex_artifact_path = root / "codex-provider-release-canary.json"
    codex_evidence_root = root / "codex-provider-release"
    codex_artifact = run_codex_provider_release_canary(
        {
            "repo_root": args.repo_root,
            "evidence_root": codex_evidence_root,
            "artifact": codex_artifact_path,
            "codex_bin": args.provider_bin,
            "source_review_status": "not_run",
            "source_review_note": _CODEX_LIGHTWEIGHT_MESSAGE,
        }
    )
    canaries = dict(codex_artifact.get("canaries") or {})
    release_artifact_path = codex_artifact.get("artifact_path") or str(codex_artifact_path)
    release_verdict = str(codex_artifact.get("verdict") or "")
    release_lane_status = "fail" if release_verdict == "red" else "warn" if release_verdict == "yellow" else "pass"
    canaries["codex_release_lane"] = _status(
        release_lane_status,
        message=_CODEX_LIGHTWEIGHT_MESSAGE,
        artifact_path=release_artifact_path,
        verdict=release_verdict,
    )
    return {
        "provider": "codex",
        "provider_version": codex_artifact.get("provider_version") or codex_artifact.get("codex_version"),
        "canaries": canaries,
        "operation_evidence": dict(codex_artifact.get("operation_evidence") or {}),
        "source_artifacts": {"codex_provider_release_canary": release_artifact_path},
    }


def _classify(canaries: dict[str, dict[str, Any]]) -> tuple[str, str | None, str]:
    first_not_run: str | None = None
    first_warn: str | None = None
    for name, canary in canaries.items():
        status = canary.get("status")
        if status == "fail":
            return "red", str(canary.get("failure_code") or name), "block_upgrade_recommendation"
        if status == "not_run" and first_not_run is None:
            first_not_run = name
        if status == "warn" and first_warn is None:
            first_warn = name
    if first_not_run:
        return "yellow", "insufficient_coverage", "investigate_before_upgrade"
    if first_warn:
        return "yellow", None, "investigate_before_upgrade"
    return "green", None, "upgrade_allowed"


def _tail_text(path: Path, *, max_chars: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _wait_for_opencode_server_url(log_path: Path, process: subprocess.Popen[str], *, timeout_secs: float) -> str:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        match = _OPENCODE_SERVER_LOG_RE.search(_tail_text(log_path))
        if match:
            return match.group("url")
        if process.poll() is not None:
            detail = _tail_text(log_path).strip()
            raise RuntimeError(f"OpenCode server exited before ready: {detail}")
        time.sleep(0.1)
    detail = _tail_text(log_path).strip()
    raise TimeoutError(f"Timed out waiting for OpenCode server URL: {detail}")


def _basic_auth_header(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")


def _request_json(
    *,
    server_url: str,
    username: str,
    password: str,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 8,
) -> Any:
    suffix = path if path.startswith("/") else f"/{path}"
    url = f"{server_url.rstrip('/')}{suffix}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": _basic_auth_header(username, password),
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {path} returned invalid JSON") from exc


def _doc_path_summary(doc: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, methods in sorted((doc.get("paths") or {}).items()):
        if not isinstance(methods, dict):
            continue
        rows.append(
            {
                "path": path,
                "methods": sorted(methods.keys()),
                "operation_ids": [
                    operation.get("operationId")
                    for operation in methods.values()
                    if isinstance(operation, dict) and operation.get("operationId")
                ],
            }
        )
    return rows


def _require_doc_operation(doc: dict[str, Any], path: str, method: str, operation_id: str) -> dict[str, Any] | None:
    operation = ((doc.get("paths") or {}).get(path) or {}).get(method.lower())
    if not isinstance(operation, dict):
        return _fail(
            "opencode_schema_missing_path",
            f"OpenCode /doc is missing {method.upper()} {path}",
            path=path,
            method=method.upper(),
            expected=operation_id,
        )
    observed = str(operation.get("operationId") or "")
    if observed != operation_id:
        return _fail(
            "opencode_schema_operation_mismatch",
            f"OpenCode /doc operation mismatch for {method.upper()} {path}",
            path=path,
            method=method.upper(),
            expected=operation_id,
            observed=observed,
        )
    return None


def _require_doc_request_property(
    doc: dict[str, Any],
    path: str,
    method: str,
    property_name: str,
    expected_type: str,
) -> dict[str, Any] | None:
    operation = ((doc.get("paths") or {}).get(path) or {}).get(method.lower())
    if not isinstance(operation, dict):
        return None
    schema = (
        (((operation.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
        if isinstance(operation.get("requestBody"), dict)
        else None
    )
    properties = schema.get("properties") if isinstance(schema, dict) else None
    prop = properties.get(property_name) if isinstance(properties, dict) else None
    if not isinstance(prop, dict):
        return _fail(
            "opencode_schema_missing_request_property",
            f"OpenCode /doc is missing request property {property_name} for {method.upper()} {path}",
            path=path,
            method=method.upper(),
            property=property_name,
            expected_type=expected_type,
        )
    observed_type = str(prop.get("type") or "")
    if observed_type != expected_type:
        return _fail(
            "opencode_schema_request_property_type_mismatch",
            f"OpenCode /doc request property {property_name} has unexpected type for {method.upper()} {path}",
            path=path,
            method=method.upper(),
            property=property_name,
            expected_type=expected_type,
            observed_type=observed_type,
        )
    return None


def _messages_contain_text(messages: Any, expected_text: str) -> bool:
    if not isinstance(messages, list):
        return False
    for item in messages:
        if not isinstance(item, dict):
            continue
        for part in item.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text") == expected_text:
                return True
    return False


def _assistant_message_contains_text(message: Any, expected_text: str) -> bool:
    if not isinstance(message, dict):
        return False
    info = message.get("info")
    if not isinstance(info, dict) or info.get("role") != "assistant":
        return False
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        # Real models may quote or lightly wrap the marker; the noReply path
        # above stays exact because OpenCode should persist user input verbatim.
        if part.get("type") == "text" and expected_text in str(part.get("text") or ""):
            return True
    return False


def _messages_contain_assistant_text(messages: Any, expected_text: str) -> bool:
    if not isinstance(messages, list):
        return False
    return any(_assistant_message_contains_text(item, expected_text) for item in messages)


def _message_info(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    info = message.get("info")
    if not isinstance(info, dict) or info.get("role") != "assistant":
        return {}
    return info


def _assistant_message_error_name(message: Any) -> str:
    info = _message_info(message)
    if not info:
        return ""
    error = info.get("error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("name") or "")


def _assistant_message_has_abort_error(message: Any, *, provider_session_id: str | None = None) -> bool:
    info = _message_info(message)
    if provider_session_id is not None and str(info.get("sessionID") or "") != provider_session_id:
        return False
    return _assistant_message_error_name(message) == "MessageAbortedError"


def _message_text_contains(item: dict[str, Any], expected_text: str) -> bool:
    for part in item.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and expected_text in str(part.get("text") or ""):
            return True
    return False


def _user_message_ids_containing_text(messages: Any, expected_text: str) -> set[str]:
    if not isinstance(messages, list):
        return set()
    ids: set[str] = set()
    for item in messages:
        if not isinstance(item, dict):
            continue
        info = item.get("info")
        if not isinstance(info, dict) or info.get("role") != "user":
            continue
        message_id = str(info.get("id") or "")
        if message_id and _message_text_contains(item, expected_text):
            ids.add(message_id)
    return ids


def _messages_contain_abort_reply_for_marker(
    messages: Any,
    *,
    marker: str,
    provider_session_id: str,
) -> bool:
    user_ids = _user_message_ids_containing_text(messages, marker)
    if not user_ids or not isinstance(messages, list):
        return False
    for item in messages:
        if not isinstance(item, dict):
            continue
        info = _message_info(item)
        if not info:
            continue
        if str(info.get("parentID") or "") not in user_ids:
            continue
        if _assistant_message_has_abort_error(item, provider_session_id=provider_session_id):
            return True
    return False


def _wait_for_user_message_marker(
    *,
    server_url: str,
    username: str,
    password: str,
    provider_session_id: str,
    workspace: Path,
    marker: str,
    timeout_secs: int,
) -> tuple[set[str], int, str | None]:
    poll_attempts = 0
    last_error: str | None = None
    deadline = time.monotonic() + min(8.0, max(2.0, timeout_secs / 10))
    while time.monotonic() < deadline:
        poll_attempts += 1
        try:
            messages = _request_json(
                server_url=server_url,
                username=username,
                password=password,
                method="GET",
                path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/message",
                query={"directory": str(workspace), "limit": "20"},
                timeout=min(10, max(1, timeout_secs)),
            )
        except RuntimeError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
            continue
        user_ids = _user_message_ids_containing_text(messages, marker)
        if user_ids:
            return user_ids, poll_attempts, last_error
        time.sleep(0.2)
    return set(), poll_attempts, last_error


def _run_opencode_prompt_async_no_reply_delivery(
    *,
    server_url: str,
    username: str,
    password: str,
    provider_session_id: str,
    workspace: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    marker = f"LONGHOUSE_OPENCODE_NOREPLY_{secrets.token_urlsafe(24)}"
    path = f"/session/{urllib.parse.quote(provider_session_id, safe='')}/prompt_async"
    try:
        _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="POST",
            path=path,
            query={"directory": str(workspace)},
            payload={"noReply": True, "parts": [{"type": "text", "text": marker}]},
        )
    except RuntimeError as exc:
        return _fail(
            "opencode_prompt_async_request_failed",
            f"OpenCode prompt_async noReply request failed: {exc}",
            provider_session_id=provider_session_id,
            request_phase="post_prompt_async",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    messages = None
    poll_attempts = 0
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        poll_attempts += 1
        try:
            messages = _request_json(
                server_url=server_url,
                username=username,
                password=password,
                method="GET",
                path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/message",
                query={"directory": str(workspace), "limit": "20"},
            )
        except RuntimeError as exc:
            return _fail(
                "opencode_prompt_async_request_failed",
                f"OpenCode session.messages request failed after prompt_async noReply input: {exc}",
                provider_session_id=provider_session_id,
                request_phase="get_session_messages",
                poll_attempts=poll_attempts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        if _messages_contain_text(messages, marker):
            return _status(
                "pass",
                provider_session_id=provider_session_id,
                message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
                observed_message_count=len(messages) if isinstance(messages, list) else None,
                poll_attempts=poll_attempts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        time.sleep(0.2)
    return _fail(
        "opencode_prompt_async_delivery_not_observed",
        "OpenCode accepted prompt_async noReply input, but session.messages did not return the marker.",
        provider_session_id=provider_session_id,
        observed_message_count=len(messages) if isinstance(messages, list) else None,
        poll_attempts=poll_attempts,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _run_opencode_assistant_response_contract(
    *,
    server_url: str,
    username: str,
    password: str,
    provider_session_id: str,
    workspace: Path,
    timeout_secs: int,
) -> dict[str, Any]:
    started = time.monotonic()
    marker = f"LONGHOUSE_OPENCODE_LIVE_{secrets.token_hex(16)}"
    prompt = f"Reply with exactly this token and no other text: {marker}"
    try:
        response = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="POST",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/message",
            query={"directory": str(workspace)},
            payload={"parts": [{"type": "text", "text": prompt}]},
            timeout=timeout_secs,
        )
    except RuntimeError as exc:
        return _fail(
            "opencode_assistant_response_request_failed",
            f"OpenCode live-token assistant response request failed: {exc}",
            provider_session_id=provider_session_id,
            request_phase="post_session_message",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    if not _assistant_message_contains_text(response, marker):
        return _fail(
            "opencode_assistant_response_marker_missing",
            "OpenCode returned an assistant response, but it did not contain the expected marker.",
            provider_session_id=provider_session_id,
            request_phase="post_session_message",
            response_role=(response.get("info") or {}).get("role") if isinstance(response, dict) else None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        messages = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="GET",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/message",
            query={"directory": str(workspace), "limit": "20"},
            timeout=timeout_secs,
        )
    except RuntimeError as exc:
        return _fail(
            "opencode_assistant_response_request_failed",
            f"OpenCode session.messages request failed after live-token assistant response: {exc}",
            provider_session_id=provider_session_id,
            request_phase="get_session_messages",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    if not _messages_contain_assistant_text(messages, marker):
        return _fail(
            "opencode_assistant_response_transcript_missing",
            "OpenCode returned the assistant marker, but session.messages did not expose it.",
            provider_session_id=provider_session_id,
            observed_message_count=len(messages) if isinstance(messages, list) else None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    info = response.get("info") if isinstance(response, dict) else {}
    tokens = info.get("tokens") if isinstance(info, dict) else None
    return _status(
        "pass",
        provider_session_id=provider_session_id,
        assistant_message_id=info.get("id") if isinstance(info, dict) else None,
        provider_id=info.get("providerID") if isinstance(info, dict) else None,
        model_id=info.get("modelID") if isinstance(info, dict) else None,
        finish=info.get("finish") if isinstance(info, dict) else None,
        cost=info.get("cost") if isinstance(info, dict) else None,
        tokens=tokens,
        observed_message_count=len(messages) if isinstance(messages, list) else None,
        message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _run_opencode_active_turn_abort_contract(
    *,
    server_url: str,
    username: str,
    password: str,
    provider_session_id: str,
    workspace: Path,
    timeout_secs: int,
) -> dict[str, Any]:
    started = time.monotonic()
    marker = f"LONGHOUSE_OPENCODE_ABORT_{secrets.token_hex(16)}"
    prompt = " ".join(
        (
            "Write exactly 500 numbered lines about this marker, one line per number,",
            f"and do not stop early: {marker}",
        )
    )
    result: dict[str, Any] = {}

    def post_message() -> None:
        try:
            result["response"] = _request_json(
                server_url=server_url,
                username=username,
                password=password,
                method="POST",
                path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/message",
                query={"directory": str(workspace)},
                payload={"parts": [{"type": "text", "text": prompt}]},
                timeout=timeout_secs,
            )
        except RuntimeError as exc:
            result["request_error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=post_message, daemon=True)
    thread.start()
    user_message_ids, pre_abort_poll_attempts, pre_abort_transcript_error = _wait_for_user_message_marker(
        server_url=server_url,
        username=username,
        password=password,
        provider_session_id=provider_session_id,
        workspace=workspace,
        marker=marker,
        timeout_secs=timeout_secs,
    )
    if not user_message_ids:
        with contextlib.suppress(RuntimeError):
            _request_json(
                server_url=server_url,
                username=username,
                password=password,
                method="POST",
                path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/abort",
                query={"directory": str(workspace)},
                payload={},
                timeout=min(10, timeout_secs),
            )
        thread.join(timeout=min(5, max(1, timeout_secs)))
        return _fail(
            "opencode_active_turn_user_message_not_observed",
            "OpenCode did not expose the active user message before abort, so in-flight abort could not be proven.",
            provider_session_id=provider_session_id,
            pre_abort_poll_attempts=pre_abort_poll_attempts,
            pre_abort_transcript_error=pre_abort_transcript_error,
            request_error=result.get("request_error"),
            message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        abort_result = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="POST",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/abort",
            query={"directory": str(workspace)},
            payload={},
            timeout=min(10, timeout_secs),
        )
    except RuntimeError as exc:
        return _fail(
            "opencode_active_turn_abort_request_failed",
            f"OpenCode session.abort failed while a message turn was in flight: {exc}",
            provider_session_id=provider_session_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    abort_ok_dict = isinstance(abort_result, dict) and abort_result.get("ok") is True
    abort_ok = abort_result is True or abort_result is None or abort_ok_dict
    if not abort_ok:
        return _fail(
            "opencode_active_turn_abort_failed",
            "OpenCode session.abort did not return a successful response shape during an active turn.",
            provider_session_id=provider_session_id,
            abort_result=abort_result,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    thread.join(timeout=max(1, timeout_secs))
    if thread.is_alive():
        return _fail(
            "opencode_active_turn_abort_did_not_settle",
            "OpenCode accepted abort during an active turn, but the in-flight message request did not settle.",
            provider_session_id=provider_session_id,
            message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    messages = None
    transcript_error: str | None = None
    try:
        messages = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="GET",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/message",
            query={"directory": str(workspace), "limit": "20"},
            timeout=timeout_secs,
        )
    except RuntimeError as exc:
        transcript_error = f"{type(exc).__name__}: {exc}"

    response = result.get("response")
    response_abort_observed = _assistant_message_has_abort_error(response, provider_session_id=provider_session_id)
    transcript_abort_observed = _messages_contain_abort_reply_for_marker(
        messages,
        marker=marker,
        provider_session_id=provider_session_id,
    )
    if not response_abort_observed or not transcript_abort_observed:
        return _fail(
            "opencode_active_turn_abort_not_observed",
            "OpenCode accepted abort during an active turn, but the aborted response was not bound to this turn.",
            provider_session_id=provider_session_id,
            pre_abort_poll_attempts=pre_abort_poll_attempts,
            pre_abort_user_message_count=len(user_message_ids),
            response_error_name=_assistant_message_error_name(response),
            response_abort_observed=response_abort_observed,
            transcript_abort_observed=transcript_abort_observed,
            request_error=result.get("request_error"),
            transcript_error=transcript_error,
            observed_message_count=len(messages) if isinstance(messages, list) else None,
            message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    return _status(
        "pass",
        provider_session_id=provider_session_id,
        response_abort_observed=response_abort_observed,
        transcript_abort_observed=transcript_abort_observed,
        response_error_name=_assistant_message_error_name(response),
        pre_abort_poll_attempts=pre_abort_poll_attempts,
        pre_abort_user_message_count=len(user_message_ids),
        observed_message_count=len(messages) if isinstance(messages, list) else None,
        message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _stop_process_group(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()


def _run_attach_shape(binary: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [binary, "attach", "--help"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("opencode_attach_help_failed", f"{type(exc).__name__}: {exc}", argv=[binary, "attach", "--help"])
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        return _fail(
            "opencode_attach_help_failed",
            "opencode attach --help failed",
            evidence=_command_evidence(result),
        )
    required_tokens = (
        "-s, --session",
        "--password",
        "--username",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_SERVER_USERNAME",
    )
    missing = [token for token in required_tokens if token not in output]
    if missing:
        return _fail(
            "opencode_attach_contract_missing",
            "opencode attach help is missing expected auth/session flags",
            missing=missing,
            evidence=_command_evidence(result),
        )
    return _status("pass", evidence=_command_evidence(result))


def _run_claude_auth_status(binary: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [binary, "auth", "status", "--json"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail(
            "claude_auth_status_failed",
            f"{type(exc).__name__}: {exc}",
            argv=[binary, "auth", "status", "--json"],
        )
    evidence = _metadata_only_command_evidence(result)
    if result.returncode != 0:
        return _status(
            "warn",
            reason="claude_auth_status_nonzero",
            message="Claude auth status is unavailable; release compatibility can still be source-reviewed.",
            evidence=evidence,
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return _fail(
            "claude_auth_status_invalid_json",
            "claude auth status --json returned invalid JSON",
            evidence=evidence,
        )
    auth_summary = {
        "loggedIn": bool(payload.get("loggedIn")),
        "authMethod": str(payload.get("authMethod") or ""),
        "apiProvider": str(payload.get("apiProvider") or ""),
        "subscriptionType": str(payload.get("subscriptionType") or ""),
    }
    # Do not publish email/org identifiers into Sauron-facing artifacts.
    if auth_summary["loggedIn"]:
        return _status("pass", auth=auth_summary)
    return _status(
        "warn",
        reason="claude_auth_not_logged_in",
        message="Claude is not logged in on this machine.",
        auth=auth_summary,
    )


def _run_claude_command_shape(binary: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [binary, "--help"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("claude_help_failed", f"{type(exc).__name__}: {exc}", argv=[binary, "--help"])
    evidence = _command_evidence(result)
    if result.returncode != 0:
        return _fail("claude_help_failed", "claude --help failed", evidence=evidence)
    output = f"{result.stdout}\n{result.stderr}"
    required_tokens = (
        "--session-id",
        "--resume",
        "--dangerously-skip-permissions",
        "--mcp-config",
        "--strict-mcp-config",
        "--permission-mode",
    )
    missing = [token for token in required_tokens if token not in output]
    if missing:
        return _fail(
            "claude_command_contract_missing",
            "claude --help is missing expected launch/session flags",
            missing=missing,
            evidence=evidence,
        )
    return _status("pass", evidence=evidence)


def _run_claude_channels_shape(binary: str) -> dict[str, Any]:
    argv = [binary, "--dangerously-load-development-channels", "server:longhouse-channel", "--help"]
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("claude_channels_probe_failed", f"{type(exc).__name__}: {exc}", argv=argv)
    output = f"{result.stdout}\n{result.stderr}"
    if (
        "unknown option --dangerously-load-development-channels" in output
        or "Unknown option '--dangerously-load-development-channels'" in output
    ):
        return _fail(
            "claude_development_channels_contract_missing",
            "Claude does not recognize the development channel flag Longhouse needs for private MCP channels.",
            evidence=_command_evidence(result),
        )
    required_tokens = ("--session-id", "--resume", "--dangerously-skip-permissions")
    missing = [token for token in required_tokens if token not in output]
    if missing:
        return _status(
            "warn",
            reason="claude_development_channels_contract_unconfirmed",
            message=_CLAUDE_CHANNEL_UNCONFIRMED_MESSAGE,
            missing=missing,
            evidence=_command_evidence(result),
        )
    return _status("pass", evidence=_command_evidence(result))


def _run_claude_pty_wrapper_shape() -> dict[str, Any]:
    if sys.platform != "darwin":
        return _status("pass", platform=sys.platform, reason="pty_wrapper_not_required")
    script_path = shutil.which("script")
    if not script_path:
        return _fail(
            "claude_detached_pty_unavailable",
            "Detached Claude launch on macOS requires script(1), but it was not found on PATH.",
        )
    return _status("pass", script_path=script_path, platform=sys.platform)


def _claude_live_token_contract_placeholder() -> dict[str, Any]:
    return _status(
        "not_run",
        reason=(
            "Claude no-token live canary proves binary/auth/channel/PTY shape only; "
            "scheduled live-token evidence must prove detached launch, active-turn steer transcript delivery, "
            "idle steer rejection, interrupt, and reattach."
        ),
    )


def _run_antigravity_command_shape(binary: str) -> dict[str, Any]:
    probes = [
        ([binary, "--help"], ("--print", "--prompt-interactive", "--conversation", "plugin")),
        ([binary, "plugin", "--help"], ("install <target>", "list", "validate")),
    ]
    evidence: list[dict[str, Any]] = []
    missing_by_probe: list[dict[str, Any]] = []
    for argv, required_tokens in probes:
        try:
            result = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _fail("antigravity_help_failed", f"{type(exc).__name__}: {exc}", argv=argv)
        output = f"{result.stdout}\n{result.stderr}"
        evidence.append(_command_evidence(result))
        if result.returncode != 0:
            return _fail("antigravity_help_failed", "Antigravity help command failed", evidence=evidence)
        missing = [token for token in required_tokens if token not in output]
        if missing:
            missing_by_probe.append({"argv": argv, "missing": missing})
    if missing_by_probe:
        return _fail(
            "antigravity_command_contract_missing",
            "Antigravity help output is missing expected CLI/plugin controls",
            missing_by_probe=missing_by_probe,
            evidence=evidence,
        )
    return _status("pass", evidence=evidence)


def _antigravity_hook_config(hook_script: Path) -> dict[str, Any]:
    command_prefix = shlex.quote(str(hook_script))
    return {
        "PreInvocation": [
            {"type": "command", "command": f"{command_prefix} PreInvocation", "timeout": 5},
        ],
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": f"{command_prefix} PreToolUse", "timeout": 5}],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": f"{command_prefix} PostToolUse", "timeout": 5}],
            },
        ],
        "PostInvocation": [
            {"type": "command", "command": f"{command_prefix} PostInvocation", "timeout": 5},
        ],
        "Stop": [
            {"type": "command", "command": f"{command_prefix} Stop", "timeout": 5},
        ],
    }


def _write_antigravity_canary_plugin(root: Path) -> tuple[Path, Path]:
    plugin_root = root / _ANTIGRAVITY_PLUGIN_NAME
    plugin_root.mkdir(parents=True, exist_ok=True)
    hook_script = plugin_root / "longhouse-antigravity-hook.sh"
    hook_script.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'case "${1:-}" in',
                "  PreInvocation) printf '{\"injectSteps\":[]}\\n' ;;",
                '  PostInvocation) printf \'{"injectSteps":[],"terminationBehavior":""}\\n\' ;;',
                '  Stop) printf \'{"decision":"allow","reason":""}\\n\' ;;',
                "  *) printf '{}\\n' ;;",
                "esac",
                "",
            ]
        ),
        encoding="utf-8",
    )
    hook_script.chmod(0o755)
    (plugin_root / "plugin.json").write_text(
        json.dumps({"name": _ANTIGRAVITY_PLUGIN_NAME}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    hooks = {_ANTIGRAVITY_PLUGIN_NAME: _antigravity_hook_config(hook_script)}
    (plugin_root / "hooks.json").write_text(json.dumps(hooks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    global_hooks_path = root / "global-hooks.json"
    global_hooks_path.write_text(json.dumps(hooks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return plugin_root, global_hooks_path


def _run_antigravity_global_hooks_contract(global_hooks_path: Path) -> dict[str, Any]:
    try:
        hooks = json.loads(global_hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fail("antigravity_global_hooks_invalid", f"{type(exc).__name__}: {exc}")
    provider_hooks = hooks.get(_ANTIGRAVITY_PLUGIN_NAME)
    if not isinstance(provider_hooks, dict):
        return _fail(
            "antigravity_global_hooks_missing",
            "Generated Antigravity global hooks config is missing the Longhouse runtime entry",
            global_hooks_path=str(global_hooks_path),
        )
    missing = [event for event in _ANTIGRAVITY_HOOK_EVENTS if event not in provider_hooks]
    if missing:
        return _fail(
            "antigravity_global_hooks_events_missing",
            "Generated Antigravity global hooks config is missing required events",
            missing=missing,
            global_hooks_path=str(global_hooks_path),
        )
    return _status(
        "pass",
        global_hooks_path=str(global_hooks_path),
        events=sorted(provider_hooks),
        note="Antigravity plugin install/list is upstream evidence; global hooks are a Longhouse config contract.",
    )


def _run_antigravity_plugin_command(
    argv: list[str],
    *,
    home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if home is not None:
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
    return subprocess.run(
        argv,
        text=True,
        capture_output=True,
        timeout=12,
        check=False,
        env=env,
    )


def _run_antigravity_plugin_contract(binary: str, root: Path) -> dict[str, Any]:
    plugin_root, _global_hooks_path = _write_antigravity_canary_plugin(root / "plugin")
    isolated_home = root / "home"

    try:
        validate = _run_antigravity_plugin_command([binary, "plugin", "validate", str(plugin_root)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("antigravity_plugin_validate_failed", f"{type(exc).__name__}: {exc}")
    if validate.returncode != 0:
        return _fail(
            "antigravity_plugin_validate_failed",
            "agy plugin validate rejected the Longhouse runtime plugin shape",
            evidence=_command_evidence(validate),
            plugin_root=str(plugin_root),
        )

    try:
        install = _run_antigravity_plugin_command(
            [binary, "plugin", "install", str(plugin_root)],
            home=isolated_home,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("antigravity_plugin_install_failed", f"{type(exc).__name__}: {exc}")
    if install.returncode != 0:
        return _fail(
            "antigravity_plugin_install_failed",
            "agy plugin install rejected the Longhouse runtime plugin shape",
            evidence=_command_evidence(install),
            plugin_root=str(plugin_root),
            isolated_home=str(isolated_home),
        )

    try:
        listed = _run_antigravity_plugin_command([binary, "plugin", "list"], home=isolated_home)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("antigravity_plugin_list_failed", f"{type(exc).__name__}: {exc}")
    if listed.returncode != 0:
        return _fail(
            "antigravity_plugin_list_failed",
            "agy plugin list failed after isolated install",
            evidence=_command_evidence(listed),
            isolated_home=str(isolated_home),
        )
    if _ANTIGRAVITY_PLUGIN_NAME not in f"{listed.stdout}\n{listed.stderr}":
        return _fail(
            "antigravity_plugin_install_not_listed",
            "agy plugin list did not show the isolated Longhouse runtime plugin install",
            evidence=_command_evidence(listed),
            isolated_home=str(isolated_home),
        )

    return _status(
        "pass",
        plugin_root=str(plugin_root),
        isolated_home=str(isolated_home),
        validate_evidence=_command_evidence(validate),
        install_evidence=_command_evidence(install),
        list_evidence=_command_evidence(listed),
        note=_ANTIGRAVITY_PLUGIN_NOTE,
    )


def run_claude_live_canary(args: argparse.Namespace, _root: Path) -> dict[str, Any]:
    binary = _resolve_provider_binary(args, "claude")
    if not binary:
        return {
            "provider": "claude",
            "provider_version": None,
            "canaries": {"binary_identity": _fail("provider_binary_not_found", "claude binary was not found on PATH")},
        }
    version, version_evidence = _run_version(binary)
    if not version:
        return {
            "provider": "claude",
            "provider_version": None,
            "canaries": {
                "binary_identity": _fail(
                    "provider_version_failed",
                    "claude --version failed",
                    path=binary,
                    evidence=version_evidence,
                )
            },
        }
    return {
        "provider": "claude",
        "provider_version": version,
        "canaries": {
            "binary_identity": _status("pass", path=binary, version=version, evidence=version_evidence),
            "auth_status": _run_claude_auth_status(binary),
            "command_shape": _run_claude_command_shape(binary),
            "channels_shape": _run_claude_channels_shape(binary),
            "detached_pty_shape": _run_claude_pty_wrapper_shape(),
            "live_token_contract": _claude_live_token_contract_placeholder(),
        },
    }


def run_opencode_live_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    binary = _resolve_provider_binary(args, "opencode")
    if not binary:
        return {
            "provider": "opencode",
            "provider_version": None,
            "canaries": {
                "binary_identity": _fail("provider_binary_not_found", "opencode binary was not found on PATH"),
            },
        }

    version, version_evidence = _run_version(binary)
    if not version:
        return {
            "provider": "opencode",
            "provider_version": None,
            "canaries": {
                "binary_identity": _fail(
                    "provider_version_failed",
                    "opencode --version failed",
                    path=binary,
                    evidence=version_evidence,
                )
            },
        }

    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    log_path = root / "opencode-server.log"
    doc_summary_path = root / "opencode-doc-paths.json"
    username = "opencode"
    password = secrets.token_urlsafe(24)
    env = os.environ.copy()
    env["OPENCODE_SERVER_USERNAME"] = username
    env["OPENCODE_SERVER_PASSWORD"] = password

    process: subprocess.Popen[str] | None = None
    canaries: dict[str, dict[str, Any]] = {
        "binary_identity": _status("pass", path=binary, version=version, evidence=version_evidence),
        "attach_command_shape": _run_attach_shape(binary),
    }
    try:
        cmd = [binary, "serve", "--hostname", "127.0.0.1", "--port", "0", "--pure"]
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        server_url = _wait_for_opencode_server_url(log_path, process, timeout_secs=args.wait_ready_secs)

        health = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="GET",
            path="/global/health",
        )
        if not isinstance(health, dict) or health.get("healthy") is not True:
            canaries["server_startup"] = _fail(
                "opencode_health_not_ready",
                "OpenCode server health check did not report healthy",
                health=health,
            )
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        canaries["server_startup"] = _status(
            "pass",
            server_url=server_url,
            health=health,
            log_path=str(log_path),
        )

        doc = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="GET",
            path="/doc",
        )
        if not isinstance(doc, dict):
            canaries["schema_probe"] = _fail("opencode_doc_invalid", "OpenCode /doc did not return an object")
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        doc_summary = _doc_path_summary(doc)
        doc_summary_path.write_text(json.dumps(doc_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        doc_hash = hashlib.sha256(json.dumps(doc_summary, sort_keys=True).encode("utf-8")).hexdigest()
        required_operations = [
            ("/global/health", "get", "global.health"),
            ("/session", "post", "session.create"),
            ("/session/{sessionID}", "get", "session.get"),
            ("/session/{sessionID}/message", "get", "session.messages"),
            ("/session/{sessionID}/prompt_async", "post", "session.prompt_async"),
            ("/session/{sessionID}/abort", "post", "session.abort"),
        ]
        if bool(getattr(args, "run_live_token_contract", False)):
            required_operations.append(("/session/{sessionID}/message", "post", "session.prompt"))
        failures = [
            failure
            for path, method, operation_id in required_operations
            if (failure := _require_doc_operation(doc, path, method, operation_id)) is not None
        ]
        request_property_failures = [
            failure
            for path, method, property_name, expected_type in [
                ("/session/{sessionID}/prompt_async", "post", "noReply", "boolean"),
            ]
            if (failure := _require_doc_request_property(doc, path, method, property_name, expected_type)) is not None
        ]
        failures.extend(request_property_failures)
        if failures:
            canaries["schema_probe"] = _fail(
                "opencode_schema_probe_failed",
                "OpenCode /doc is missing required Longhouse server-bridge operations",
                failures=failures,
                doc_summary_path=str(doc_summary_path),
                doc_summary_sha256=doc_hash,
            )
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        required_operation_payloads = []
        for path, method, operation_id in required_operations:
            required_operation_payloads.append({"path": path, "method": method, "operation_id": operation_id})
        canaries["schema_probe"] = _status(
            "pass",
            required_operations=required_operation_payloads,
            doc_path_count=len(doc_summary),
            doc_summary_path=str(doc_summary_path),
            doc_summary_sha256=doc_hash,
        )

        session = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="POST",
            path="/session",
            query={"directory": str(workspace)},
            payload={"title": "Longhouse OpenCode live canary"},
        )
        provider_session_id = str(session.get("id") or "") if isinstance(session, dict) else ""
        if not provider_session_id:
            canaries["session_create"] = _fail(
                "opencode_session_create_missing_id",
                "OpenCode session.create returned no session id",
                session=session,
            )
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        canaries["session_create"] = _status(
            "pass",
            provider_session_id=provider_session_id,
            cost=session.get("cost") if isinstance(session, dict) else None,
            tokens=session.get("tokens") if isinstance(session, dict) else None,
        )

        fetched_session = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="GET",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}",
        )
        if not isinstance(fetched_session, dict) or fetched_session.get("id") != provider_session_id:
            canaries["session_get"] = _fail(
                "opencode_session_get_mismatch",
                "OpenCode session.get did not return the created session",
                session=fetched_session,
            )
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        canaries["session_get"] = _status("pass", provider_session_id=provider_session_id)
        canaries["prompt_async_no_reply_delivery"] = _run_opencode_prompt_async_no_reply_delivery(
            server_url=server_url,
            username=username,
            password=password,
            provider_session_id=provider_session_id,
            workspace=workspace,
        )
        if canaries["prompt_async_no_reply_delivery"]["status"] != "pass":
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}

        if bool(getattr(args, "run_live_token_contract", False)):
            canaries["assistant_response_contract"] = _run_opencode_assistant_response_contract(
                server_url=server_url,
                username=username,
                password=password,
                provider_session_id=provider_session_id,
                workspace=workspace,
                timeout_secs=int(getattr(args, "live_token_timeout_secs", 120) or 120),
            )
        else:
            canaries["assistant_response_contract"] = _status(
                "not_run",
                reason=" ".join(
                    (
                        "Pass --run-live-token-contract to spend tokens and prove assistant response execution",
                        "plus active-turn abort.",
                    )
                ),
            )

        if canaries["assistant_response_contract"].get("status") == "pass":
            canaries["active_turn_abort_contract"] = _run_opencode_active_turn_abort_contract(
                server_url=server_url,
                username=username,
                password=password,
                provider_session_id=provider_session_id,
                workspace=workspace,
                timeout_secs=int(getattr(args, "live_token_timeout_secs", 120) or 120),
            )
            if canaries["active_turn_abort_contract"].get("status") != "pass":
                return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        elif bool(getattr(args, "run_live_token_contract", False)):
            canaries["active_turn_abort_contract"] = _status(
                "not_run",
                reason="Assistant response contract did not pass, so active-turn abort proof was not run.",
            )
        else:
            canaries["active_turn_abort_contract"] = _status(
                "not_run",
                reason="Pass --run-live-token-contract to prove active-turn abort.",
            )

        abort_result = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="POST",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/abort",
        )
        abort_ok_dict = isinstance(abort_result, dict) and abort_result.get("ok") is True
        abort_ok = abort_result is True or abort_result is None or abort_ok_dict
        if not abort_ok:
            canaries["session_abort"] = _fail(
                "opencode_session_abort_failed",
                "OpenCode session.abort did not return a successful response shape",
                result=abort_result,
            )
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        canaries["session_abort"] = _status("pass", provider_session_id=provider_session_id)
        if canaries.get("assistant_response_contract", {}).get("status") == "pass":
            canaries["prompt_async_execution_contract"] = _status(
                "pass",
                canary="assistant_response_contract",
                reason="Live-token assistant response execution and transcript binding passed.",
            )
            canaries["process_restart_reattach_contract"] = _status(
                "not_run",
                reason="Future live-token evidence still must prove process-restart reattach.",
            )
            canaries["active_turn_abort_and_reattach_contract"] = _status(
                "not_run",
                reason=(
                    "Active-turn abort is tracked separately by active_turn_abort_contract; "
                    "process-restart reattach remains future proof."
                ),
            )
        else:
            canaries["prompt_async_execution_contract"] = _status(
                "not_run",
                reason=(
                    "OpenCode no-token live canary proves prompt_async noReply delivery into session.messages; "
                    "pass --run-live-token-contract to prove assistant response execution and transcript binding. "
                    "Remaining future proof must cover active-turn abort and process-restart reattach."
                ),
            )
        return {"provider": "opencode", "provider_version": version, "canaries": canaries}
    except Exception as exc:  # noqa: BLE001
        canaries["live_contract"] = _fail(
            "opencode_live_canary_exception",
            f"{type(exc).__name__}: {exc}",
            log_path=str(log_path),
            log_tail=_tail_text(log_path),
        )
        return {"provider": "opencode", "provider_version": version, "canaries": canaries}
    finally:
        _stop_process_group(process)


def run_antigravity_live_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    binary = _resolve_provider_binary(args, "agy")
    if not binary:
        return {
            "provider": "antigravity",
            "provider_version": None,
            "canaries": {"binary_identity": _fail("provider_binary_not_found", "agy binary was not found on PATH")},
        }

    version, version_evidence = _run_version(binary)
    if not version:
        return {
            "provider": "antigravity",
            "provider_version": None,
            "canaries": {
                "binary_identity": _fail(
                    "provider_version_failed",
                    "agy --version failed",
                    path=binary,
                    evidence=version_evidence,
                )
            },
        }

    return {
        "provider": "antigravity",
        "provider_version": version,
        "canaries": {
            "binary_identity": _status("pass", path=binary, version=version, evidence=version_evidence),
            "command_shape": _run_antigravity_command_shape(binary),
            "plugin_contract": _run_antigravity_plugin_contract(binary, root),
            "global_hooks_contract": _run_antigravity_global_hooks_contract(root / "plugin" / "global-hooks.json"),
            "loop_invocation_contract": _status(
                "not_run",
                reason=(
                    "This no-token canary proves agy plugin/config drift only. "
                    "A real upstream agy loop must invoke PreInvocation/PostInvocation/Stop before "
                    "Antigravity send can be promoted as behavior-proven."
                ),
            ),
        },
    }


def run_provider(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    if args.provider == "codex":
        return run_codex_live_canary(args, root)
    if args.provider == "claude":
        return run_claude_live_canary(args, root)
    if args.provider == "opencode":
        return run_opencode_live_canary(args, root)
    if args.provider == "antigravity":
        return run_antigravity_live_canary(args, root)
    return {
        "provider": args.provider,
        "provider_version": None,
        "canaries": {
            "live_contract": _status(
                "not_run",
                reason=f"{args.provider} live canary is not implemented in this dispatcher yet.",
            )
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=default_repo_root())
    parser.add_argument("--provider", choices=["codex", "claude", "opencode", "antigravity"], required=True)
    parser.add_argument("--provider-bin")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--wait-ready-secs", type=float, default=15.0)
    parser.add_argument(
        "--run-live-token-contract",
        action="store_true",
        help="For OpenCode, spend small model calls to prove assistant response execution and active-turn abort.",
    )
    parser.add_argument("--live-token-timeout-secs", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    return parser


def run_provider_live_canary(args: argparse.Namespace | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(args, Mapping):
        args = argparse.Namespace(**dict(args))
    else:
        args = argparse.Namespace(**vars(args))
    args.repo_root = Path(args.repo_root).expanduser().resolve()
    if args.evidence_root is not None:
        args.evidence_root = Path(args.evidence_root).expanduser()
    if args.artifact is not None:
        args.artifact = Path(args.artifact).expanduser()
    if not hasattr(args, "run_live_token_contract"):
        args.run_live_token_contract = False
    if not hasattr(args, "live_token_timeout_secs"):
        args.live_token_timeout_secs = 120
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = args.evidence_root or _default_evidence_root(args.repo_root, args.provider, timestamp)
    artifact_path = args.artifact or evidence_root / "provider-live-canary.json"
    evidence_root.mkdir(parents=True, exist_ok=True)

    provider_result = run_provider(args, evidence_root)
    canaries = provider_result["canaries"]
    manifest_operation_evidence = _provider_operation_evidence(args.repo_root, provider_result["provider"], canaries)
    operation_evidence = dict(provider_result.get("operation_evidence") or manifest_operation_evidence)
    verdict, failure_code, recommendation = _classify(canaries)
    artifact = {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "artifact_kind": "provider_live_canary",
        "provider": provider_result["provider"],
        "provider_version": provider_result.get("provider_version"),
        "generated_at": _now_iso(),
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": recommendation,
        "canaries": canaries,
        "artifact_path": str(artifact_path),
        "evidence_root": str(evidence_root),
    }
    if operation_evidence:
        artifact["operation_evidence"] = operation_evidence
    if provider_result.get("source_artifacts"):
        artifact["source_artifacts"] = provider_result["source_artifacts"]
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    artifact = run_provider_live_canary(args)
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["verdict"] != "red" else 1


if __name__ == "__main__":
    raise SystemExit(main())
