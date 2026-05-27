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
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

PROVIDER_STATUS_SCHEMA_VERSION = 1
_OPENCODE_SERVER_LOG_RE = re.compile(r"opencode server listening on (?P<url>http://127\.0\.0\.1:\d+)")
_ANTIGRAVITY_PLUGIN_NAME = "longhouse-runtime"
_ANTIGRAVITY_HOOK_EVENTS = ("PreInvocation", "PreToolUse", "PostToolUse", "PostInvocation", "Stop")
_GAP_OPERATION_STATUSES = {"fail", "missing", "not_run", "skipped", "stale"}


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


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
    path = repo_root / "server/zerg/config/managed_provider_contracts.json"
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
    source: str | None = None,
    message: str | None = None,
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
        level="none" if status == "fail" else None,
        source=source,
        message=message or (detail or {}).get("message"),
        failure_code=(detail or {}).get("failure_code"),
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
        message=(
            "API-surface proof: attach command exposes session and auth flags; "
            "process-restart reattach is future proof."
        ),
    )
    if reattach:
        evidence["reattach"] = reattach

    send_failure = _schema_probe_failed_for(canaries, "/session/{sessionID}/prompt_async", "session.prompt_async")
    if send_failure:
        evidence["send_input"] = _operation_entry(
            contract,
            "send_input",
            status="fail",
            canary="opencode_prompt_async_schema",
            level="none",
            failure_code=send_failure.get("failure_code") or "opencode_prompt_async_schema_failed",
            message=send_failure.get("message"),
        )
    elif canaries.get("schema_probe", {}).get("status") == "pass":
        evidence["send_input"] = _operation_entry(
            contract,
            "send_input",
            status="pass",
            canary="opencode_prompt_async_schema",
            canaries=["schema_probe"],
            message=(
                "API-surface proof: /doc exposes session.prompt_async; token-spending transcript proof is future work."
            ),
        )

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
            message="API-surface proof: abort endpoint accepted a request against a created session.",
        )
        if interrupt:
            evidence["interrupt"] = interrupt

    return evidence


def _antigravity_operation_evidence(
    contract: dict[str, Any], canaries: dict[str, dict[str, Any]]
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
            message="Claude recognized the development channel flag, but launch help did not confirm the session-control shape.",
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
        note=(
            "agy may report hook components as skipped here; "
            "Longhouse wires Antigravity hooks through the global hooks config."
        ),
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
                "binary_identity": _fail("provider_binary_not_found", "opencode binary was not found on PATH")
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
            ("/session/{sessionID}/prompt_async", "post", "session.prompt_async"),
            ("/session/{sessionID}/abort", "post", "session.abort"),
        ]
        failures = [
            failure
            for path, method, operation_id in required_operations
            if (failure := _require_doc_operation(doc, path, method, operation_id)) is not None
        ]
        if failures:
            canaries["schema_probe"] = _fail(
                "opencode_schema_probe_failed",
                "OpenCode /doc is missing required Longhouse server-bridge operations",
                failures=failures,
                doc_summary_path=str(doc_summary_path),
                doc_summary_sha256=doc_hash,
            )
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        canaries["schema_probe"] = _status(
            "pass",
            required_operations=[
                {"path": path, "method": method, "operation_id": operation_id}
                for path, method, operation_id in required_operations
            ],
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

        abort_result = _request_json(
            server_url=server_url,
            username=username,
            password=password,
            method="POST",
            path=f"/session/{urllib.parse.quote(provider_session_id, safe='')}/abort",
        )
        abort_ok = (
            abort_result is True
            or abort_result is None
            or (isinstance(abort_result, dict) and abort_result.get("ok") is True)
        )
        if not abort_ok:
            canaries["session_abort"] = _fail(
                "opencode_session_abort_failed",
                "OpenCode session.abort did not return a successful response shape",
                result=abort_result,
            )
            return {"provider": "opencode", "provider_version": version, "canaries": canaries}
        canaries["session_abort"] = _status("pass", provider_session_id=provider_session_id)
        canaries["prompt_async_execution_contract"] = _status(
            "not_run",
            reason=(
                "OpenCode no-token live canary proves server schema/session/abort shape only; "
                "scheduled live-token evidence must prove prompt_async execution, transcript binding, "
                "active-turn abort, and reattach."
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
    parser.add_argument("--repo-root", type=Path, default=_repo_root_from_script())
    parser.add_argument("--provider", choices=["codex", "claude", "opencode", "antigravity"], required=True)
    parser.add_argument("--provider-bin")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--wait-ready-secs", type=float, default=15.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = args.evidence_root or args.repo_root / ".build/canaries/provider-live" / args.provider / timestamp
    artifact_path = args.artifact or evidence_root / "provider-live-canary.json"
    evidence_root.mkdir(parents=True, exist_ok=True)

    provider_result = run_provider(args, evidence_root)
    canaries = provider_result["canaries"]
    operation_evidence = _provider_operation_evidence(args.repo_root, provider_result["provider"], canaries)
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
        "evidence_root": str(evidence_root),
    }
    if operation_evidence:
        artifact["operation_evidence"] = operation_evidence
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["verdict"] != "red" else 1


if __name__ == "__main__":
    raise SystemExit(main())
