#!/usr/bin/env python3
"""Codex provider release canary artifact generator.

This is the Sauron-facing wrapper around Longhouse's managed Codex contract
checks. It emits one JSON artifact with pass/warn/fail status per canary and
keeps raw evidence under an isolated evidence directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.qa.repo_root import default_repo_root

ACTIVE_THREAD_ERROR = "No active thread is available."
PROVIDER_STATUS_SCHEMA_VERSION = 1
PROVIDER_LIVE_CANARY_ARTIFACT_KIND = "provider_live_canary"
PROVIDER_RELEASE_CANARY_ARTIFACT_KIND = "provider_release_canary"
_GAP_OPERATION_STATUSES = {"fail", "missing", "not_run", "skipped", "stale"}
_RAW_FRESH_REMOTE_MESSAGE = " ".join(
    (
        "Raw fresh remote canary observed provider protocol/terminal output;",
        "warnings preserve proof but require review.",
    )
)
TERMINAL_TURN_METHODS = {
    "turn/completed",
    "turn/failed",
    "turn/interrupted",
    "turn/cancelled",
}
FINGERPRINT_RESPONSE_METHODS = {
    "initialize",
    "thread/start",
    "thread/resume",
    "thread/read",
    "thread/list",
    "turn/start",
}
FINGERPRINT_NOTIFICATION_METHODS = {
    "thread/started",
    "turn/started",
    *TERMINAL_TURN_METHODS,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _redact_argv(argv: Any, secrets: list[str] | None = None) -> Any:
    if not isinstance(argv, list):
        return argv
    secrets = [secret for secret in (secrets or []) if secret]
    redacted: list[Any] = []
    redact_next = False
    for item in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        redacted.append("<redacted>" if item in secrets else item)
        if item == "--agents-token":
            redact_next = True
    return redacted


def _redact_text(text: str, secrets: list[str] | None = None) -> str:
    redacted = text
    for secret in secrets or []:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _command_evidence(result: subprocess.CompletedProcess[str], *, secrets: list[str] | None = None) -> dict[str, Any]:
    return {
        "argv": _redact_argv(result.args, secrets),
        "returncode": result.returncode,
        "stdout": _redact_text(result.stdout[-4000:], secrets),
        "stderr": _redact_text(result.stderr[-4000:], secrets),
    }


def _status(status: str, **fields: Any) -> dict[str, Any]:
    data = {"status": status}
    data.update(fields)
    return data


def _fail(code: str, message: str, **fields: Any) -> dict[str, Any]:
    data = {"status": "fail", "failure_code": code, "message": message}
    data.update(fields)
    return data


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = result.stdout.strip()
    if not text:
        raise ValueError("command produced no stdout")
    return json.loads(text)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return _value_type(value)
    if isinstance(value, dict):
        return {str(key): _shape(value[key], depth=depth + 1) for key in sorted(value)}
    if isinstance(value, list):
        if not value:
            return []
        return [_shape(value[0], depth=depth + 1)]
    return _value_type(value)


def protocol_fingerprints_from_jsonl(path: Path) -> dict[str, Any]:
    """Return redacted protocol-shape fingerprints from a canary JSONL log."""
    pending: dict[str, str] = {}
    responses: dict[str, Any] = {}
    notifications: dict[str, Any] = {}
    server_requests: dict[str, Any] = {}
    response_errors: dict[str, Any] = {}

    if not path.exists():
        return {
            "status": "missing",
            "path": str(path),
            "responses": responses,
            "notifications": notifications,
            "server_requests": server_requests,
            "response_errors": response_errors,
        }

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        request_id = payload.get("id")
        method = payload.get("method")
        direction = row.get("direction")
        if direction == "client_request" and request_id is not None and isinstance(method, str):
            pending[str(request_id)] = method
            continue
        if direction != "server_message":
            continue
        if request_id is not None and "result" in payload:
            request_method = pending.get(str(request_id))
            if request_method in FINGERPRINT_RESPONSE_METHODS and request_method not in responses:
                responses[request_method] = _shape(payload.get("result"))
            continue
        if request_id is not None and "error" in payload:
            request_method = pending.get(str(request_id), f"request#{request_id}")
            response_errors.setdefault(request_method, _shape(payload.get("error")))
            continue
        if isinstance(method, str):
            if request_id is not None:
                server_requests.setdefault(method, _shape(payload.get("params")))
            elif method in FINGERPRINT_NOTIFICATION_METHODS and method not in notifications:
                notifications[method] = _shape(payload.get("params"))

    return {
        "status": "ok",
        "path": str(path),
        "responses": responses,
        "notifications": notifications,
        "server_requests": server_requests,
        "response_errors": response_errors,
    }


def _git_commit(repo_root: Path) -> str | None:
    result = _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _resolve_executable(value: str | None, fallback_name: str) -> str | None:
    if value:
        return value
    return shutil.which(fallback_name)


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
    level: str | None = None,
    source: str | None = None,
    message: str | None = None,
    failure_code: str | None = None,
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
    if failure_code:
        entry["failure_code"] = failure_code
    if message:
        entry["message"] = message
    if target.get("next"):
        entry["next"] = target.get("next")
    return {key: value for key, value in entry.items() if value is not None}


def _canary_operation_entry(
    contract: dict[str, Any],
    operation: str,
    *,
    canary_name: str,
    canary: dict[str, Any],
    pass_statuses: set[str] | None = None,
    source: str | None = None,
    message: str | None = None,
) -> dict[str, Any] | None:
    pass_statuses = pass_statuses or {"pass"}
    status = str(canary.get("status") or "")
    if status == "fail":
        return _operation_entry(
            contract,
            operation,
            status="fail",
            canary=canary_name,
            level="none",
            source=source,
            message=canary.get("message") or message,
            failure_code=canary.get("failure_code"),
        )
    if status in pass_statuses:
        return _operation_entry(
            contract,
            operation,
            status=status,
            canary=canary_name,
            source=source,
            message=message or canary.get("message"),
        )
    return None


def build_operation_evidence(
    args: argparse.Namespace,
    canaries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    contract = _provider_contract(args.repo_root, "codex")
    if contract is None:
        return {}
    evidence: dict[str, dict[str, Any]] = {}

    binary_identity = canaries.get("binary_identity") or {}
    if binary_identity.get("status") == "fail":
        evidence["launch_local"] = _operation_entry(
            contract,
            "launch_local",
            status="fail",
            canary="binary_identity",
            level="none",
            message=binary_identity.get("message"),
            failure_code=binary_identity.get("failure_code"),
        )

    managed_tui_attach = canaries.get("managed_tui_attach") or {}
    launch_local = _canary_operation_entry(
        contract,
        "launch_local",
        canary_name="managed_tui_attach",
        canary=managed_tui_attach,
    )
    if launch_local and "launch_local" not in evidence:
        evidence["launch_local"] = launch_local
    reattach = _canary_operation_entry(
        contract,
        "reattach",
        canary_name="managed_tui_attach",
        canary=managed_tui_attach,
        message="Managed TUI attach proves the remote attach surface; process-restart resume is future proof.",
    )
    if reattach:
        evidence["reattach"] = reattach

    detached_ui = _canary_operation_entry(
        contract,
        "launch_remote",
        canary_name="detached_ui",
        canary=canaries.get("detached_ui") or {},
    )
    if detached_ui:
        evidence["launch_remote"] = detached_ui

    tail_output = _canary_operation_entry(
        contract,
        "tail_output",
        canary_name="raw_fresh_remote",
        canary=canaries.get("raw_fresh_remote") or {},
        pass_statuses={"pass", "warn"},
        message=_RAW_FRESH_REMOTE_MESSAGE,
    )
    if tail_output:
        evidence["tail_output"] = tail_output

    return evidence


def _forbidden_codex_path(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    real = os.path.realpath(path).replace("\\", "/")
    candidates = [normalized, real]
    for candidate in candidates:
        if candidate.endswith("/longhouse-codex") or candidate == "longhouse-codex":
            return "longhouse_codex_launcher"
        if "/.longhouse/runtimes/codex" in candidate:
            return "longhouse_owned_runtime"
    return None


def run_binary_identity(args: argparse.Namespace) -> dict[str, Any]:
    codex_bin = _resolve_executable(args.codex_bin, "codex")
    if not codex_bin:
        return _fail("codex_not_found", "codex binary was not found on PATH")

    override = os.environ.get("LONGHOUSE_CODEX_BIN")
    if override and not args.allow_codex_bin_override:
        return _fail(
            "codex_bin_override_set",
            "LONGHOUSE_CODEX_BIN is set outside an explicit debug lane",
            env_var="LONGHOUSE_CODEX_BIN",
            value=override,
            path=codex_bin,
        )

    forbidden = _forbidden_codex_path(codex_bin)
    if forbidden:
        return _fail(
            forbidden,
            "canary would exercise a forbidden Longhouse-owned Codex path",
            path=codex_bin,
            real_path=os.path.realpath(codex_bin),
        )

    result = _run([codex_bin, "--version"], timeout=20)
    if result.returncode != 0:
        return _fail(
            "codex_version_failed",
            "codex --version failed",
            path=codex_bin,
            real_path=os.path.realpath(codex_bin),
            evidence=_command_evidence(result),
        )

    return _status(
        "pass",
        path=codex_bin,
        real_path=os.path.realpath(codex_bin),
        version=result.stdout.strip() or result.stderr.strip(),
    )


def run_static_contract(args: argparse.Namespace) -> dict[str, Any]:
    script = args.repo_root / "scripts/qa/check-managed-codex-contract.sh"
    if not script.exists():
        return _status(
            "not_run",
            reason="managed Codex static contract guard is only available from a source checkout",
            expected_script=str(script),
        )
    env = os.environ.copy()
    env["MANAGED_CODEX_CONTRACT_ROOT"] = str(args.repo_root)
    result = _run(["bash", str(script)], cwd=args.repo_root, env=env, timeout=60)
    if result.returncode != 0:
        return _fail(
            "static_contract_failed",
            "managed Codex static contract guard failed",
            evidence=_command_evidence(result),
        )
    return _status("pass", evidence=result.stdout.strip())


def run_fake_app_server_unit(args: argparse.Namespace) -> dict[str, Any]:
    cargo_bin = _resolve_executable(args.cargo_bin, "cargo")
    if not cargo_bin:
        return _fail("cargo_not_found", "cargo binary was not found")
    result = _run(
        [
            cargo_bin,
            "test",
            "--manifest-path",
            str(args.repo_root / "engine/Cargo.toml"),
            "--bin",
            "longhouse-engine",
            "canary_runs_against_fake_codex_app_server",
        ],
        cwd=args.repo_root,
        timeout=args.fake_app_server_timeout_secs,
    )
    if result.returncode != 0:
        return _fail(
            "fake_app_server_unit_failed",
            "fake app-server unit contract test failed",
            evidence=_command_evidence(result),
        )
    return _status("pass", evidence=result.stdout[-1200:])


def run_raw_fresh_remote(args: argparse.Namespace, evidence_root: Path, codex_bin: str) -> dict[str, Any]:
    engine = _resolve_executable(args.engine, "longhouse-engine")
    if not engine:
        return _fail("engine_not_found", "longhouse-engine binary was not found")

    root = evidence_root / "raw-fresh-remote"
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    summary_path = root / "summary.json"
    jsonl_path = root / "canary.jsonl"
    remote_tui_log = root / "remote-tui.log"

    command = [
        engine,
        "codex-app-server-canary",
        "--prompt",
        "Reply exactly CANARY_OK.",
        "--cwd",
        str(workspace),
        "--codex-bin",
        codex_bin,
        "--app-server-transport",
        "websocket",
        "--spawn-remote-tui",
        "--approval-policy",
        "never",
        "--sandbox",
        "read-only",
        "--event-timeout-secs",
        str(args.canary_timeout_secs),
        "--remote-tui-grace-ms",
        str(args.remote_tui_grace_ms),
        "--remote-tui-subscribe-phase",
        "after_rollout",
        "--remote-tui-log",
        str(remote_tui_log),
        "--log-jsonl",
        str(jsonl_path),
        "--json",
    ]
    if args.model:
        command.extend(["--model", args.model])

    result = _run(command, cwd=args.repo_root, timeout=args.canary_timeout_secs + 20)
    summary_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        return _fail(
            "raw_fresh_remote_failed",
            "raw fresh remote TUI canary command failed",
            evidence_root=str(root),
            evidence=_command_evidence(result),
        )

    remote_log = remote_tui_log.read_text(encoding="utf-8", errors="replace") if remote_tui_log.exists() else ""
    try:
        summary = _load_json_stdout(result)
    except ValueError as exc:
        return _fail(
            "raw_fresh_remote_bad_json",
            str(exc),
            evidence_root=str(root),
            evidence=_command_evidence(result),
        )
    if ACTIVE_THREAD_ERROR in remote_log:
        return _status(
            "warn",
            evidence=f"raw fresh remote TUI showed: {ACTIVE_THREAD_ERROR}",
            evidence_root=str(root),
            summary=summary,
            protocol_fingerprints=protocol_fingerprints_from_jsonl(jsonl_path),
        )
    return _status(
        "pass",
        evidence_root=str(root),
        summary=summary,
        protocol_fingerprints=protocol_fingerprints_from_jsonl(jsonl_path),
    )


def _bridge_state_root(isolation_root: Path) -> Path:
    return isolation_root / "codex-bridge"


def _start_bridge(
    args: argparse.Namespace,
    *,
    evidence_root: Path,
    codex_bin: str,
    launch_mode: str,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str], Path]:
    engine = _resolve_executable(args.engine, "longhouse-engine")
    if not engine:
        raise RuntimeError("longhouse-engine binary was not found")
    if not args.api_url or not args.agents_token:
        raise RuntimeError("--api-url and --agents-token are required for managed bridge canaries")

    session_id = str(uuid.uuid4())
    isolation_root = Path(tempfile.mkdtemp(prefix=f"lhx-{launch_mode[:1]}-", dir="/tmp"))
    workspace = isolation_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    log_file = evidence_root / f"bridge-{launch_mode}-{session_id}.log"

    create_initial_thread = launch_mode == "detached_ui"
    command = [
        engine,
        "codex-bridge",
        "start",
        "--session-id",
        session_id,
        "--cwd",
        str(workspace),
        "--url",
        args.api_url,
        "--codex-bin",
        codex_bin,
        "--isolation-root",
        str(isolation_root),
        "--log-file",
        str(log_file),
        "--auto-approve",
        "--approval-policy",
        "never",
        "--sandbox",
        "read-only",
        "--start-timeout-secs",
        str(args.bridge_start_timeout_secs),
        "--json",
    ]
    if create_initial_thread:
        command.append("--create-initial-thread")
    if launch_mode == "detached_ui":
        command.extend(["--launch-mode", "detached-ui"])
    if args.model:
        command.extend(["--model", args.model])

    env = os.environ.copy()
    env["LONGHOUSE_CODEX_BRIDGE_TOKEN"] = args.agents_token
    result = _run(command, cwd=args.repo_root, env=env, timeout=args.bridge_start_timeout_secs + 20)
    if result.returncode != 0:
        raise RuntimeError(json.dumps(_command_evidence(result, secrets=[args.agents_token])))
    try:
        summary = _load_json_stdout(result)
    except ValueError as exc:
        raise RuntimeError(
            json.dumps(
                {
                    "error": str(exc),
                    "evidence": _command_evidence(result, secrets=[args.agents_token]),
                }
            )
        ) from exc
    return summary, result, isolation_root


def _stop_bridge(args: argparse.Namespace, session_id: str, isolation_root: Path) -> dict[str, Any]:
    engine = _resolve_executable(args.engine, "longhouse-engine")
    if not engine:
        return {"attempted": False, "error": "engine_not_found"}
    result = _run(
        [
            engine,
            "codex-bridge",
            "stop",
            "--session-id",
            session_id,
            "--state-root",
            str(_bridge_state_root(isolation_root)),
            "--reason",
            "provider_release_canary",
        ],
        cwd=args.repo_root,
        timeout=30,
    )
    return {"attempted": True, "evidence": _command_evidence(result)}


def _record_terminal_session(
    args: argparse.Namespace,
    command: list[str],
    recording_path: Path,
) -> subprocess.CompletedProcess[str]:
    script_bin = _resolve_executable(args.script_bin, "script")
    timeout_bin = _resolve_executable(args.timeout_bin, "timeout") or shutil.which("gtimeout")
    if not script_bin:
        raise RuntimeError("script binary was not found")
    if not timeout_bin:
        raise RuntimeError("timeout/gtimeout binary was not found")

    wrapped = [
        timeout_bin,
        f"{args.tui_record_secs}s",
        script_bin,
        "-q",
        str(recording_path),
        *command,
    ]
    return _run(wrapped, cwd=args.repo_root, timeout=args.tui_record_secs + 10)


def run_managed_tui_attach(args: argparse.Namespace, evidence_root: Path, codex_bin: str) -> dict[str, Any]:
    root = evidence_root / "managed-tui-attach"
    root.mkdir(parents=True, exist_ok=True)
    isolation_root: Path | None = None
    session_id: str | None = None
    try:
        summary, start_result, isolation_root = _start_bridge(
            args,
            evidence_root=root,
            codex_bin=codex_bin,
            launch_mode="tui",
        )
        session_id = str(summary.get("session_id") or "")
        ws_url = str(summary.get("ws_url") or "")
        state_file = Path(str(summary.get("state_file") or ""))
        if not ws_url or not state_file.exists():
            return _fail(
                "managed_tui_attach_incomplete_start",
                "managed bridge start did not return ws_url and state_file",
                evidence_root=str(root),
                summary=summary,
                start=_command_evidence(start_result),
            )
        state = _read_json(state_file)
        if state.get("launch_mode") != "tui":
            return _fail(
                "managed_tui_attach_wrong_launch_mode",
                "managed TUI bridge did not persist launch_mode=tui",
                evidence_root=str(root),
                state=state,
            )

        recording = root / "attach-tui.tty"
        terminal_command = [
            codex_bin,
            "-c",
            "check_for_update_on_startup=false",
            "--enable",
            "tui_app_server",
            "--remote",
            ws_url,
            "--no-alt-screen",
        ]
        tui_result = _record_terminal_session(args, terminal_command, recording)
        recording_text = recording.read_text(encoding="utf-8", errors="replace") if recording.exists() else ""
        if tui_result.returncode not in (0, 124):
            return _fail(
                "managed_tui_attach_failed",
                "managed TUI attach recording command failed",
                evidence_root=str(root),
                evidence=_command_evidence(tui_result),
            )
        if ACTIVE_THREAD_ERROR in recording_text:
            return _fail(
                "managed_tui_attach_active_thread_error",
                f"managed TUI attach showed: {ACTIVE_THREAD_ERROR}",
                evidence_root=str(root),
                recording=str(recording),
            )
        attached_state = _read_json(state_file)
        attached_thread_id = str(attached_state.get("thread_id") or "").strip()
        if not attached_thread_id:
            return _fail(
                "managed_tui_attach_missing_thread",
                "managed TUI attach did not materialize a Codex thread",
                evidence_root=str(root),
                recording=str(recording),
                state=attached_state,
            )
        return _status(
            "pass",
            thread_id=attached_thread_id,
            ws_url=ws_url,
            state_file=str(state_file),
            recording=str(recording),
            evidence_root=str(root),
        )
    except Exception as exc:  # noqa: BLE001 - canary artifact should keep failure evidence
        return _fail("managed_tui_attach_exception", str(exc), evidence_root=str(root))
    finally:
        if session_id and isolation_root:
            stop = _stop_bridge(args, session_id, isolation_root)
            (root / "stop.json").write_text(json.dumps(stop, indent=2), encoding="utf-8")


def run_detached_ui(args: argparse.Namespace, evidence_root: Path, codex_bin: str) -> dict[str, Any]:
    root = evidence_root / "detached-ui"
    root.mkdir(parents=True, exist_ok=True)
    isolation_root: Path | None = None
    session_id: str | None = None
    try:
        summary, start_result, isolation_root = _start_bridge(
            args,
            evidence_root=root,
            codex_bin=codex_bin,
            launch_mode="detached_ui",
        )
        session_id = str(summary.get("session_id") or "")
        thread_id = str(summary.get("thread_id") or "")
        state_file = Path(str(summary.get("state_file") or ""))
        if not thread_id or not state_file.exists():
            return _fail(
                "detached_ui_incomplete_start",
                "detached-ui bridge start did not return thread_id and state_file",
                evidence_root=str(root),
                summary=summary,
                start=_command_evidence(start_result),
            )
        state = _read_json(state_file)
        if state.get("launch_mode") != "detached_ui":
            return _fail(
                "detached_ui_wrong_launch_mode",
                "detached-ui bridge did not persist launch_mode=detached_ui",
                evidence_root=str(root),
                state=state,
            )
        ipc_socket = state_file.with_suffix(".sock")
        if not ipc_socket.exists():
            return _fail(
                "detached_ui_ipc_missing",
                "detached-ui bridge did not expose its IPC socket",
                evidence_root=str(root),
                state_file=str(state_file),
                expected_ipc_socket=str(ipc_socket),
            )
        return _status(
            "pass",
            thread_id=thread_id,
            state_file=str(state_file),
            ipc_socket=str(ipc_socket),
            evidence_root=str(root),
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("detached_ui_exception", str(exc), evidence_root=str(root))
    finally:
        if session_id and isolation_root:
            stop = _stop_bridge(args, session_id, isolation_root)
            (root / "stop.json").write_text(json.dumps(stop, indent=2), encoding="utf-8")


def classify_artifact(
    canaries: dict[str, dict[str, Any]],
    source_review: dict[str, Any],
) -> tuple[str, str | None, str]:
    source_status = source_review.get("status")
    if source_status == "fail":
        return "red", "source_review_failed", "block_upgrade_recommendation"
    if source_status in {"not_run", None}:
        return "yellow", "insufficient_coverage", "investigate_before_upgrade"
    first_warn: str | None = None
    first_not_run: str | None = None
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
    if source_status == "warn" or first_warn:
        return "yellow", None, "investigate_before_upgrade"
    return "green", None, "upgrade_allowed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=default_repo_root())
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--engine")
    parser.add_argument("--codex-bin")
    parser.add_argument("--provider-version")
    parser.add_argument("--cargo-bin")
    parser.add_argument("--script-bin")
    parser.add_argument("--timeout-bin")
    parser.add_argument("--model")
    parser.add_argument("--api-url")
    parser.add_argument("--agents-token")
    parser.add_argument("--allow-codex-bin-override", action="store_true")
    parser.add_argument("--skip-binary-identity", action="store_true")
    parser.add_argument("--skip-static-contract", action="store_true")
    parser.add_argument("--run-fake-app-server", action="store_true")
    parser.add_argument("--run-raw-fresh-remote", action="store_true")
    parser.add_argument("--run-managed-tui-attach", action="store_true")
    parser.add_argument("--run-detached-ui", action="store_true")
    parser.add_argument("--run-all-live", action="store_true")
    parser.add_argument(
        "--source-review-status",
        choices=["not_run", "pass", "warn", "fail"],
        default="not_run",
        help="Sauron source-review result. not_run keeps the artifact yellow with insufficient_coverage.",
    )
    parser.add_argument(
        "--source-review-note",
        default="Sauron source review should fill this section before publishing a release recommendation.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--canary-timeout-secs", type=int, default=90)
    parser.add_argument("--fake-app-server-timeout-secs", type=int, default=120)
    parser.add_argument("--bridge-start-timeout-secs", type=int, default=30)
    parser.add_argument("--remote-tui-grace-ms", type=int, default=3000)
    parser.add_argument("--tui-record-secs", type=int, default=8)
    return parser


def _coerce_args(args: argparse.Namespace | Mapping[str, Any]) -> argparse.Namespace:
    if isinstance(args, Mapping):
        defaults = vars(build_parser().parse_args([]))
        defaults.update(dict(args))
        args = argparse.Namespace(**defaults)
    else:
        args = argparse.Namespace(**vars(args))
    args.repo_root = Path(args.repo_root).expanduser().resolve()
    if args.evidence_root is not None:
        args.evidence_root = Path(args.evidence_root).expanduser().resolve()
    if args.artifact is not None:
        args.artifact = Path(args.artifact).expanduser().resolve()
    return args


def run_codex_provider_release_canary(args: argparse.Namespace | Mapping[str, Any]) -> dict[str, Any]:
    args = _coerce_args(args)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = args.evidence_root or args.repo_root / ".build/canaries/codex" / timestamp
    evidence_root.mkdir(parents=True, exist_ok=True)
    artifact_path = args.artifact or evidence_root / "provider-release-canary.json"

    if args.run_all_live:
        args.run_fake_app_server = True
        args.run_raw_fresh_remote = True
        args.run_managed_tui_attach = True
        args.run_detached_ui = True

    canaries: dict[str, dict[str, Any]] = {}
    canaries["binary_identity"] = (
        _status("not_run", reason="--skip-binary-identity", version=args.provider_version)
        if args.skip_binary_identity
        else run_binary_identity(args)
    )
    codex_bin = str(canaries["binary_identity"].get("path") or args.codex_bin or "codex")

    if args.skip_static_contract:
        canaries["static_contract"] = _status("not_run", reason="--skip-static-contract")
    else:
        canaries["static_contract"] = run_static_contract(args)

    canaries["fake_app_server"] = (
        run_fake_app_server_unit(args)
        if args.run_fake_app_server
        else _status("not_run", reason="pass --run-fake-app-server to exercise this canary")
    )
    canaries["raw_fresh_remote"] = (
        run_raw_fresh_remote(args, evidence_root, codex_bin)
        if args.run_raw_fresh_remote
        else _status("not_run", reason="pass --run-raw-fresh-remote to exercise this canary")
    )
    canaries["managed_tui_attach"] = (
        run_managed_tui_attach(args, evidence_root, codex_bin)
        if args.run_managed_tui_attach
        else _status("not_run", reason="pass --run-managed-tui-attach to exercise this canary")
    )
    canaries["detached_ui"] = (
        run_detached_ui(args, evidence_root, codex_bin)
        if args.run_detached_ui
        else _status("not_run", reason="pass --run-detached-ui to exercise this canary")
    )

    source_review = {
        "status": args.source_review_status,
        "note": args.source_review_note,
    }
    operation_evidence = build_operation_evidence(args, canaries)
    verdict, failure_code, recommendation = classify_artifact(canaries, source_review)
    provider_version = args.provider_version or canaries["binary_identity"].get("version")
    artifact = {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "artifact_kind": PROVIDER_RELEASE_CANARY_ARTIFACT_KIND,
        "provider": "codex",
        "generated_at": _now_iso(),
        "provider_version": provider_version,
        "codex_version": provider_version,
        "codex_bin": canaries["binary_identity"].get("path"),
        "longhouse_commit": _git_commit(args.repo_root),
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": recommendation,
        "source_review": source_review,
        "canaries": canaries,
        "evidence_root": str(evidence_root),
    }
    if operation_evidence:
        artifact["operation_evidence"] = operation_evidence
    artifact["artifact_path"] = str(artifact_path)
    _write_json(artifact_path, artifact)
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    artifact = run_codex_provider_release_canary(args)

    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"codex provider release canary: {artifact['verdict']}")
        print(f"artifact: {artifact['artifact_path']}")
        print(f"evidence_root: {artifact['evidence_root']}")
        if artifact.get("failure_code"):
            print(f"failure_code: {artifact['failure_code']}")

    return 1 if artifact["verdict"] == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
