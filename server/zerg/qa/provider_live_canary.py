#!/usr/bin/env python3
"""No-token upstream managed-provider canaries.

These canaries exercise the installed upstream provider binary directly for
binary identity, command/schema shape, and local control-route compatibility.
Token-spending provider behavior belongs to the explicit release-canary lane,
not the daily provider-live publish path.
"""

from __future__ import annotations

import argparse
import base64
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
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.provider_live_proof import LIVE_PROOF_ARTIFACT_KIND
from zerg.provider_live_proof import SUPPORTED_LIVE_PROOF_PROVIDERS
from zerg.qa.repo_root import provider_live_evidence_base
from zerg.services.managed_provider_contracts import ManagedProviderContract
from zerg.services.managed_provider_contracts import contract_for_provider

PROVIDER_STATUS_SCHEMA_VERSION = 1
_OPENCODE_SERVER_LOG_RE = re.compile(r"opencode server listening on (?P<url>http://127\.0\.0\.1:\d+)")
_ANTIGRAVITY_PLUGIN_NAME = "longhouse-runtime"
_ANTIGRAVITY_HOOK_EVENTS = ("PreInvocation", "PreToolUse", "PostToolUse", "PostInvocation", "Stop")
SUPPORTED_PROVIDERS = SUPPORTED_LIVE_PROOF_PROVIDERS
_GAP_OPERATION_STATUSES = {"fail", "missing", "skipped", "stale"}
_OPENCODE_REATTACH_MESSAGE = " ".join(
    (
        "Process-restart proof: a fresh OpenCode server recovered",
        "the created provider session and marker transcript.",
    )
)
_OPENCODE_PROMPT_ASYNC_MESSAGE = " ".join(
    (
        "No-token behavior proof: prompt_async accepted a noReply input and",
        "session.messages returned the delivered marker;",
        "provider-response proof belongs to release canaries.",
    )
)
_CLAUDE_CHANNEL_UNCONFIRMED_MESSAGE = (
    "Claude recognized the development channel flag, but launch help did not confirm the session-control shape."
)
_ANTIGRAVITY_PLUGIN_NOTE = (
    "agy may report hook components as skipped here; Longhouse wires Antigravity hooks through the global hooks config."
)
_SIMPLE_OPERATION_GROUPS = {
    "claude": (
        (
            "launch_local",
            ("binary_identity", "command_shape", "channels_shape", "detached_pty_shape"),
            "claude_launch_local_no_token",
        ),
    ),
    "antigravity": (
        (
            "launch_local",
            ("binary_identity", "command_shape", "plugin_contract", "global_hooks_contract"),
            "antigravity_launch_local_no_token",
        ),
    ),
}


def _default_evidence_root(provider: str, timestamp: str) -> Path:
    return provider_live_evidence_base() / provider / timestamp


def _reserve_default_evidence_root(path: Path) -> Path:
    candidate = path
    suffix = 0
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            suffix += 1
            candidate = path.with_name(f"{path.name}-{suffix}")


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


def _provider_binary_identity(
    args: argparse.Namespace,
    binary_name: str,
) -> tuple[str | None, str | None, dict[str, Any]]:
    binary = _resolve_provider_binary(args, binary_name)
    if not binary:
        return (
            None,
            None,
            _fail(
                "provider_binary_not_found",
                f"{binary_name} binary was not found on PATH",
            ),
        )
    version, evidence = _run_version(binary)
    if not version:
        return (
            None,
            None,
            _fail(
                "provider_version_failed",
                f"{binary_name} --version failed",
                path=binary,
                evidence=evidence,
            ),
        )
    return binary, version, _status("pass", path=binary, version=version, evidence=evidence)


def _operation_entry(
    contract: ManagedProviderContract,
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
    target = dict(contract.operation_evidence_for(operation))
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
    contract: ManagedProviderContract,
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
        message=(detail or {}).get("message") if status == "fail" else message or (detail or {}).get("message"),
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


def _simple_operation_evidence(
    contract: ManagedProviderContract,
    canaries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for operation, required, canary_name in _SIMPLE_OPERATION_GROUPS.get(contract.provider, ()):
        entry = _entry_from_canary_group(
            contract,
            operation,
            canaries=canaries,
            required=list(required),
            canary_name=canary_name,
        )
        if entry:
            evidence[operation] = entry
    return evidence


def _opencode_operation_evidence(
    contract: ManagedProviderContract,
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

    if (canaries.get("process_restart_reattach_contract") or {}).get("status") in {"pass", "fail", "warn"}:
        reattach = _entry_from_canary_group(
            contract,
            "reattach",
            canaries=canaries,
            required=["binary_identity", "attach_command_shape", "process_restart_reattach_contract"],
            canary_name="opencode_process_restart_reattach_contract",
            level="live_no_token",
            message=_OPENCODE_REATTACH_MESSAGE,
        )
        if reattach:
            evidence["reattach"] = reattach
    else:
        reattach = _entry_from_canary_group(
            contract,
            "reattach",
            canaries=canaries,
            required=["binary_identity", "attach_command_shape"],
            canary_name="opencode_attach_surface",
            message="API-surface proof: attach command exposes session and auth flags.",
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
    elif canaries.get("prompt_async_no_reply_delivery"):
        send_input = _entry_from_canary_group(
            contract,
            "send_input",
            canaries=canaries,
            required=["binary_identity", "schema_probe", "prompt_async_no_reply_delivery"],
            canary_name="opencode_prompt_async_no_reply_delivery",
            level="live_no_token",
            source="longhouse provider-live canary --provider opencode prompt_async noReply delivery",
            message=_OPENCODE_PROMPT_ASYNC_MESSAGE,
        )
        if send_input:
            evidence["send_input"] = send_input
        transcript_binding = _entry_from_canary_group(
            contract,
            "transcript_binding",
            canaries=canaries,
            required=["binary_identity", "schema_probe", "prompt_async_no_reply_delivery"],
            canary_name="opencode_prompt_async_no_reply_delivery",
            level="live_no_token",
            source="longhouse provider-live canary --provider opencode session.messages noReply marker",
            message="No-token transcript proof: session.messages returned the delivered prompt_async marker.",
        )
        if transcript_binding:
            evidence["transcript_binding"] = transcript_binding

    interrupt_failure = _schema_probe_failed_for(canaries, "/session/{sessionID}/abort", "session.abort")
    if interrupt_failure:
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
            level="live_no_token",
            source="longhouse provider-live canary --provider opencode abort endpoint",
            message="API-surface proof: abort endpoint accepted a request against a created session.",
        )
        if interrupt:
            evidence["interrupt"] = interrupt

    return evidence


def _provider_operation_evidence(provider: str, canaries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contract = contract_for_provider(provider)
    if contract is None:
        return {}
    if provider in _SIMPLE_OPERATION_GROUPS:
        return _simple_operation_evidence(contract, canaries)
    if provider == "opencode":
        return _opencode_operation_evidence(contract, canaries)
    return {}


def _classify(canaries: dict[str, dict[str, Any]]) -> tuple[str, str | None, str]:
    first_warn: str | None = None
    for name, canary in canaries.items():
        status = canary.get("status")
        if status == "fail":
            return "red", str(canary.get("failure_code") or name), "block_upgrade_recommendation"
        if status == "warn" and first_warn is None:
            first_warn = name
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


def _start_opencode_server_process(
    *,
    binary: str,
    workspace: Path,
    env: dict[str, str],
    log_path: Path,
    wait_ready_secs: float,
) -> tuple[subprocess.Popen[str], str]:
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
    try:
        server_url = _wait_for_opencode_server_url(log_path, process, timeout_secs=wait_ready_secs)
    except Exception:
        _stop_process_group(process)
        raise
    return process, server_url


def _run_opencode_process_restart_reattach_contract(
    *,
    binary: str,
    env: dict[str, str],
    process: subprocess.Popen[str],
    server_url: str,
    username: str,
    password: str,
    provider_session_id: str,
    workspace: Path,
    restart_log_path: Path,
    wait_ready_secs: float,
) -> tuple[dict[str, Any], subprocess.Popen[str] | None, str]:
    started = time.monotonic()
    marker = f"LONGHOUSE_OPENCODE_REATTACH_{secrets.token_hex(16)}"
    try:
        _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="POST",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/prompt_async",
            query={"directory": str(workspace)},
            payload={"noReply": True, "parts": [{"type": "text", "text": marker}]},
        )
    except RuntimeError as exc:
        return (
            _fail(
                "opencode_reattach_marker_request_failed",
                f"OpenCode prompt_async marker request failed before restart reattach proof: {exc}",
                provider_session_id=provider_session_id,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
            process,
            server_url,
        )

    user_message_ids, poll_attempts, transcript_error = _wait_for_user_message_marker(
        server_url=server_url,
        username=username,
        password=password,
        provider_session_id=provider_session_id,
        workspace=workspace,
        marker=marker,
        timeout_secs=8,
    )
    if not user_message_ids:
        return (
            _fail(
                "opencode_reattach_marker_not_observed_before_restart",
                "OpenCode did not expose the marker before restart, so reattach recovery could not be proven.",
                provider_session_id=provider_session_id,
                poll_attempts=poll_attempts,
                transcript_error=transcript_error,
                message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
            process,
            server_url,
        )

    _stop_process_group(process)
    process = None
    try:
        process, restarted_server_url = _start_opencode_server_process(
            binary=binary,
            workspace=workspace,
            env=env,
            log_path=restart_log_path,
            wait_ready_secs=wait_ready_secs,
        )
        health = _request_json(
            server_url=restarted_server_url,
            username=username,
            password=password,
            method="GET",
            path="/global/health",
        )
        if not isinstance(health, dict) or health.get("healthy") is not True:
            return (
                _fail(
                    "opencode_reattach_health_not_ready",
                    "Restarted OpenCode server health check did not report healthy.",
                    provider_session_id=provider_session_id,
                    health=health,
                    restarted_server_url=restarted_server_url,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                ),
                process,
                restarted_server_url,
            )
        fetched_session = _request_json(
            server_url=restarted_server_url,
            username=username,
            password=password,
            method="GET",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}",
        )
        if not isinstance(fetched_session, dict) or fetched_session.get("id") != provider_session_id:
            return (
                _fail(
                    "opencode_reattach_session_get_mismatch",
                    "Restarted OpenCode server did not recover the created provider session.",
                    provider_session_id=provider_session_id,
                    session=fetched_session,
                    restarted_server_url=restarted_server_url,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                ),
                process,
                restarted_server_url,
            )
        messages = _request_json(
            server_url=restarted_server_url,
            username=username,
            password=password,
            method="GET",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/message",
            query={"directory": str(workspace), "limit": "20"},
        )
    except (RuntimeError, TimeoutError, OSError) as exc:
        return (
            _fail(
                "opencode_process_restart_reattach_failed",
                f"OpenCode process-restart reattach proof failed: {exc}",
                provider_session_id=provider_session_id,
                restarted_server_log_path=str(restart_log_path),
                restarted_server_log_tail=_tail_text(restart_log_path),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
            process,
            server_url,
        )

    if not _messages_contain_text(messages, marker):
        return (
            _fail(
                "opencode_reattach_transcript_marker_missing",
                "Restarted OpenCode server recovered the session but not the marker transcript.",
                provider_session_id=provider_session_id,
                observed_message_count=len(messages) if isinstance(messages, list) else None,
                message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
                restarted_server_url=restarted_server_url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
            process,
            restarted_server_url,
        )

    return (
        _status(
            "pass",
            provider_session_id=provider_session_id,
            restarted_server_url=restarted_server_url,
            restarted_server_log_path=str(restart_log_path),
            observed_message_count=len(messages) if isinstance(messages, list) else None,
            message_marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            pre_restart_poll_attempts=poll_attempts,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        ),
        process,
        restarted_server_url,
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


def _antigravity_plugin_argv(binary: str, *args: str) -> list[str]:
    """Build an agy plugin command that still works under isolated HOME.

    The maintainer's dogfood binary may be a tiny wrapper that resolves the real agy as
    ``$HOME/.local/bin/agy``. The plugin contract intentionally changes HOME to
    isolate Antigravity config writes, so run that wrapper's underlying binary
    directly when the wrapper shape is detected.
    """

    try:
        path = Path(binary).expanduser()
        text = path.read_text(encoding="utf-8", errors="ignore")[:512] if path.is_file() else ""
    except OSError:
        text = ""
    if "$HOME/.local/bin/agy" in text and "--dangerously-skip-permissions" in text:
        direct = Path.home() / ".local" / "bin" / "agy"
        if direct.is_file() and os.access(direct, os.X_OK):
            return [str(direct), "--dangerously-skip-permissions", *args]
    return [binary, *args]


def _run_antigravity_plugin_contract(binary: str, root: Path) -> dict[str, Any]:
    plugin_root, _global_hooks_path = _write_antigravity_canary_plugin(root / "plugin")
    isolated_home = root / "home"

    try:
        validate = _run_antigravity_plugin_command(
            _antigravity_plugin_argv(
                binary,
                "plugin",
                "validate",
                str(plugin_root),
            ),
        )
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
            _antigravity_plugin_argv(binary, "plugin", "install", str(plugin_root)),
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
        listed = _run_antigravity_plugin_command(_antigravity_plugin_argv(binary, "plugin", "list"), home=isolated_home)
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


def _invoke_antigravity_hook(
    *,
    hook_script: Path,
    event: str,
    payload: dict[str, Any],
    env: Mapping[str, str],
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    result = subprocess.run(
        [str(hook_script), event],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
        env=dict(env),
    )
    parsed: dict[str, Any] | None = None
    if result.returncode == 0:
        try:
            loaded = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            parsed = loaded
    return result, parsed


def _run_antigravity_hook_inbox_contract(root: Path) -> dict[str, Any]:
    """Prove Longhouse's shipped Antigravity hook script handles inbox claims.

    This is intentionally not a real `agy` loop proof. It verifies the staged
    Longhouse hook and inbox mechanics in an isolated home so the provider-live
    artifact can expose hook-contract drift without advertising remote send.
    """

    try:
        from zerg.cli.antigravity import _ANTIGRAVITY_HOOK_SCRIPT_NAME
        from zerg.cli.antigravity import _ensure_antigravity_runtime_plugin
        from zerg.cli.antigravity_channel import antigravity_inbox_dir
        from zerg.cli.antigravity_channel import antigravity_state_dir
        from zerg.cli.antigravity_channel import enqueue_antigravity_message
        from zerg.cli.antigravity_channel import wait_for_antigravity_message_claim
    except Exception as exc:
        return _fail("antigravity_hook_import_failed", f"{type(exc).__name__}: {exc}")

    contract_root = root / "hook-inbox-contract"
    config_dir = contract_root / ".claude"
    antigravity_cli_root = contract_root / ".gemini" / "antigravity-cli"
    global_hooks_path = contract_root / ".gemini" / "config" / "hooks.json"
    workspace = contract_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        plugin_root = _ensure_antigravity_runtime_plugin(
            config_dir=config_dir,
            antigravity_cli_root=antigravity_cli_root,
            engine_path="/bin/true",
            global_hooks_path=global_hooks_path,
        )
    except Exception as exc:
        return _fail("antigravity_hook_stage_failed", f"{type(exc).__name__}: {exc}")

    hook_script = plugin_root / _ANTIGRAVITY_HOOK_SCRIPT_NAME
    if not hook_script.is_file():
        return _fail(
            "antigravity_hook_script_missing",
            "Staged Antigravity runtime plugin did not include the Longhouse hook script",
            hook_script=str(hook_script),
        )

    session_id = f"antigravity-provider-live-{secrets.token_hex(6)}"
    inbox_dir = antigravity_inbox_dir(session_id, config_dir)
    state_dir = antigravity_state_dir(config_dir)
    env = os.environ.copy()
    env.update(
        {
            "LONGHOUSE_HOOK_PYTHON": sys.executable,
            "LONGHOUSE_ENGINE": "/bin/true",
            "LONGHOUSE_MANAGED_SESSION_ID": session_id,
            "LONGHOUSE_ANTIGRAVITY_INBOX_DIR": str(inbox_dir),
            "LONGHOUSE_ANTIGRAVITY_STATE_DIR": str(state_dir),
            "PATH": os.defpath,
        }
    )

    def event_payload(step: int) -> dict[str, Any]:
        return {
            "conversationId": "antigravity-provider-live-canary",
            "workspacePaths": [str(workspace)],
            "transcriptPath": str(contract_root / "transcript.jsonl"),
            "stepIdx": step,
        }

    def enqueue(text: str) -> dict[str, Any]:
        return enqueue_antigravity_message(session_id=session_id, text=text, config_dir=config_dir)

    pre_text = f"LONGHOUSE_ANTIGRAVITY_PRE_{secrets.token_hex(8)}"
    post_text = f"LONGHOUSE_ANTIGRAVITY_POST_{secrets.token_hex(8)}"
    stop_text = f"LONGHOUSE_ANTIGRAVITY_STOP_{secrets.token_hex(8)}"

    pre_message = enqueue(pre_text)
    pre_result, pre_response = _invoke_antigravity_hook(
        hook_script=hook_script,
        event="PreInvocation",
        payload=event_payload(1),
        env=env,
    )
    expected_pre = {"injectSteps": [{"userMessage": pre_text}]}
    if pre_response != expected_pre:
        return _fail(
            "antigravity_preinvocation_injection_failed",
            "PreInvocation did not claim pending Longhouse inbox input",
            expected=expected_pre,
            observed=pre_response,
            evidence=_command_evidence(pre_result),
        )
    pre_claim = wait_for_antigravity_message_claim(
        session_id=session_id,
        message_id=str(pre_message["message_id"]),
        timeout_secs=0,
        config_dir=config_dir,
    )
    if not pre_claim or pre_claim.get("hook_event") != "PreInvocation":
        return _fail(
            "antigravity_preinvocation_claim_missing",
            "PreInvocation returned injection but did not persist a matching claim",
            claim=pre_claim,
        )

    post_message = enqueue(post_text)
    post_result, post_response = _invoke_antigravity_hook(
        hook_script=hook_script,
        event="PostInvocation",
        payload=event_payload(2),
        env=env,
    )
    expected_post = {
        "injectSteps": [{"userMessage": post_text}],
        "terminationBehavior": "force_continue",
    }
    if post_response != expected_post:
        return _fail(
            "antigravity_postinvocation_injection_failed",
            "PostInvocation did not claim pending Longhouse inbox input with force_continue",
            expected=expected_post,
            observed=post_response,
            evidence=_command_evidence(post_result),
        )
    post_claim = wait_for_antigravity_message_claim(
        session_id=session_id,
        message_id=str(post_message["message_id"]),
        timeout_secs=0,
        config_dir=config_dir,
    )
    if not post_claim or post_claim.get("hook_event") != "PostInvocation":
        return _fail(
            "antigravity_postinvocation_claim_missing",
            "PostInvocation returned injection but did not persist a matching claim",
            claim=post_claim,
        )

    enqueue(stop_text)
    stop_result, stop_response = _invoke_antigravity_hook(
        hook_script=hook_script,
        event="Stop",
        payload={**event_payload(3), "fullyIdle": True},
        env=env,
    )
    expected_stop = {
        "decision": "continue",
        "reason": "Longhouse queued input is waiting in the managed Antigravity inbox.",
    }
    if stop_response != expected_stop:
        return _fail(
            "antigravity_stop_continue_failed",
            "Stop did not continue when Longhouse inbox input was pending",
            expected=expected_stop,
            observed=stop_response,
            evidence=_command_evidence(stop_result),
        )

    empty_session_id = f"{session_id}-empty"
    empty_env = dict(env)
    empty_env["LONGHOUSE_MANAGED_SESSION_ID"] = empty_session_id
    empty_env["LONGHOUSE_ANTIGRAVITY_INBOX_DIR"] = str(antigravity_inbox_dir(empty_session_id, config_dir))
    empty_env["LONGHOUSE_ANTIGRAVITY_STATE_DIR"] = str(antigravity_state_dir(config_dir))
    empty_stop_result, empty_stop_response = _invoke_antigravity_hook(
        hook_script=hook_script,
        event="Stop",
        payload={**event_payload(4), "fullyIdle": True},
        env=empty_env,
    )
    expected_empty_stop = {"decision": "allow", "reason": ""}
    if empty_stop_response != expected_empty_stop:
        return _fail(
            "antigravity_stop_empty_queue_failed",
            "Stop should not continue when no Longhouse inbox input is pending",
            expected=expected_empty_stop,
            observed=empty_stop_response,
            evidence=_command_evidence(empty_stop_result),
        )

    return _status(
        "pass",
        hook_script=str(hook_script),
        config_dir=str(config_dir),
        global_hooks_path=str(global_hooks_path),
        pre_injection=pre_response,
        post_injection=post_response,
        stop_decision=stop_response,
        empty_stop_decision=empty_stop_response,
        pre_claim_event=pre_claim.get("hook_event"),
        post_claim_event=post_claim.get("hook_event"),
        note=(
            "Longhouse hook-inbox mechanics passed in an isolated script-level proof; "
            "real agy send promotion is owned by "
            "scripts/qa/provider-control-e2e-canary.py --provider antigravity --antigravity-real-agy-send."
        ),
    )


def run_claude_live_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    binary, version, binary_identity = _provider_binary_identity(args, "claude")
    if binary is None or version is None:
        return {"provider": "claude", "provider_version": None, "canaries": {"binary_identity": binary_identity}}
    canaries = {
        "binary_identity": binary_identity,
        "command_shape": _run_claude_command_shape(binary),
        "channels_shape": _run_claude_channels_shape(binary),
        "detached_pty_shape": _run_claude_pty_wrapper_shape(),
    }
    return {
        "provider": "claude",
        "provider_version": version,
        "canaries": canaries,
    }


def run_opencode_live_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    binary, version, binary_identity = _provider_binary_identity(args, "opencode")
    if binary is None or version is None:
        return {"provider": "opencode", "provider_version": None, "canaries": {"binary_identity": binary_identity}}

    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    log_path = root / "opencode-server.log"
    restart_log_path = root / "opencode-server-restart.log"
    doc_summary_path = root / "opencode-doc-paths.json"
    username = "opencode"
    password = secrets.token_urlsafe(24)
    env = os.environ.copy()
    env["OPENCODE_SERVER_USERNAME"] = username
    env["OPENCODE_SERVER_PASSWORD"] = password

    process: subprocess.Popen[str] | None = None
    canaries: dict[str, dict[str, Any]] = {
        "binary_identity": binary_identity,
        "attach_command_shape": _run_attach_shape(binary),
    }
    try:
        process, server_url = _start_opencode_server_process(
            binary=binary,
            workspace=workspace,
            env=env,
            log_path=log_path,
            wait_ready_secs=args.wait_ready_secs,
        )

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
            ("/session/{sessionID}/message", "post", "session.prompt"),
            ("/session/{sessionID}/prompt_async", "post", "session.prompt_async"),
            ("/session/{sessionID}/abort", "post", "session.abort"),
        ]
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

        (
            canaries["process_restart_reattach_contract"],
            restarted_process,
            restarted_server_url,
        ) = _run_opencode_process_restart_reattach_contract(
            binary=binary,
            env=env,
            process=process,
            server_url=server_url,
            username=username,
            password=password,
            provider_session_id=provider_session_id,
            workspace=workspace,
            restart_log_path=restart_log_path,
            wait_ready_secs=args.wait_ready_secs,
        )
        process = restarted_process
        server_url = restarted_server_url
        if canaries["process_restart_reattach_contract"]["status"] != "pass":
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}

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
    binary, version, binary_identity = _provider_binary_identity(args, "agy")
    if binary is None or version is None:
        return {"provider": "antigravity", "provider_version": None, "canaries": {"binary_identity": binary_identity}}

    canaries = {
        "binary_identity": binary_identity,
        "command_shape": _run_antigravity_command_shape(binary),
        "plugin_contract": _run_antigravity_plugin_contract(binary, root),
        "global_hooks_contract": _run_antigravity_global_hooks_contract(root / "plugin" / "global-hooks.json"),
        "hook_inbox_claim_contract": _run_antigravity_hook_inbox_contract(root),
    }

    return {"provider": "antigravity", "provider_version": version, "canaries": canaries}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=list(SUPPORTED_PROVIDERS), required=True)
    parser.add_argument("--provider-bin")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--wait-ready-secs", type=float, default=15.0)
    parser.add_argument("--json", action="store_true")
    return parser


def run_provider_live_canary(args: argparse.Namespace | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(args, Mapping):
        args = argparse.Namespace(**dict(args))
    else:
        args = argparse.Namespace(**vars(args))
    if args.evidence_root is not None:
        args.evidence_root = Path(args.evidence_root).expanduser()
    if args.artifact is not None:
        args.artifact = Path(args.artifact).expanduser()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if args.evidence_root is None:
        evidence_root = _reserve_default_evidence_root(_default_evidence_root(args.provider, timestamp))
    else:
        evidence_root = args.evidence_root
        evidence_root.mkdir(parents=True, exist_ok=True)
    artifact_path = args.artifact or evidence_root / "provider-live-canary.json"

    if args.provider == "claude":
        provider_result = run_claude_live_canary(args, evidence_root)
    elif args.provider == "opencode":
        provider_result = run_opencode_live_canary(args, evidence_root)
    elif args.provider == "antigravity":
        provider_result = run_antigravity_live_canary(args, evidence_root)
    else:
        raise ValueError(f"unsupported provider: {args.provider}")
    canaries = provider_result["canaries"]
    operation_evidence = _provider_operation_evidence(provider_result["provider"], canaries)
    verdict, failure_code, recommendation = _classify(canaries)
    artifact = {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "artifact_kind": LIVE_PROOF_ARTIFACT_KIND,
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
