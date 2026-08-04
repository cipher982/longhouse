#!/usr/bin/env python3
"""Direct stock-provider Helm Resume producer shared by non-Codex adapters.

The provider-specific modules only declare registration.  This module owns the
one black-box transaction: the shipped ``longhouse`` facade launches a real
provider TUI in a PTY, the Runtime Host transcript proves provider activity,
the old owner is stopped or lost, and the same facade performs native Resume.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pty
import pwd
import select
import signal
import socket
import subprocess
import sys
import termios
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa.provider_resume_oracles import native_resume_assertions
from zerg.qa.resume_assurance import ProducerRegistration

QUALIFICATION_SANDBOX_ENV = "LONGHOUSE_QUALIFICATION_SANDBOX"
QUALIFICATION_HOME_ENV = "LONGHOUSE_QUALIFICATION_HOME"
QUALIFICATION_SANDBOX_PROFILE = "provider-qualification-bwrap-v3"


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    producer_id: str
    executable_module: str
    binary_flag: str
    resume_flag: str
    credential_binding_id: str
    state_patterns: tuple[str, ...]


SPECS = {
    "claude": ProviderSpec(
        provider="claude",
        producer_id="claude.native_resume.v1",
        executable_module="zerg.qa.claude_native_resume",
        binary_flag="--claude-bin",
        resume_flag="--resume",
        credential_binding_id="claude_provider_token",
        state_patterns=(
            ".claude/channels/longhouse/sessions/*.json",
            ".longhouse/managed-local/contracts/claude/*.json",
            "managed-local/contracts/claude/*.json",
        ),
    ),
    "cursor": ProviderSpec(
        provider="cursor",
        producer_id="cursor.native_resume.v1",
        executable_module="zerg.qa.cursor_native_resume",
        binary_flag="--cursor-bin",
        resume_flag="--resume-session",
        credential_binding_id="cursor_provider_token",
        state_patterns=(
            ".longhouse/managed-local/cursor-helm/*.json",
            "managed-local/cursor-helm/*.json",
        ),
    ),
    "opencode": ProviderSpec(
        provider="opencode",
        producer_id="opencode.native_resume.v1",
        executable_module="zerg.qa.opencode_native_resume",
        binary_flag="--opencode-bin",
        resume_flag="--resume-session",
        credential_binding_id="opencode_provider_token",
        state_patterns=(
            ".claude/managed-local/opencode-server/*.json",
            "managed-local/opencode-server/*.json",
            ".longhouse/managed-local/opencode/bridge/sessions/*.json",
            "managed-local/opencode/bridge/sessions/*.json",
        ),
    ),
}


def registration_for(provider: str) -> ProducerRegistration:
    spec = SPECS[provider]
    return ProducerRegistration(
        producer_id=spec.producer_id,
        producer_revision=1,
        scenario_id="helm_cold_resume",
        scenario_revision=4,
        assertion_cells=(
            ("native_provider_resume_proven", "clean_exit"),
            ("native_provider_resume_proven", "process_loss"),
        ),
        providers=(provider,),
        platforms=("linux",),
        architectures=("x86_64", "aarch64"),
        modes=("helm",),
        evidence_classes=("live_token",),
        observed_activity=(
            "provider_neutral_resume_intent",
            "native_resume_command",
            "post_resume_provider_activity",
            "stale_input_rejected",
            "concurrent_resume_refused",
            "artifact_secret_scan_passed",
        ),
        acquisition_methods=("staged_release", "observed_install"),
        credential_binding_ids=(spec.credential_binding_id, "runtime_host_control"),
        sandbox_policy="provider-qualification-bwrap-v3",
        network_policy="shared_provider_egress",
        required_artifacts=(
            "provider_binary_receipt",
            "resume_intent_receipt",
            "initial_bridge_state",
            "initial_transcript",
            "native_resume_terminal_recording",
            "resumed_bridge_state",
            "resumed_transcript",
            "process_transition_receipt",
            "stale_input_receipt",
            "concurrent_resume_receipt",
            "cleanup_receipt",
        ),
        required_cleanup=(
            "old_owner_dead",
            "final_bridge_stopped",
            "final_socket_absent",
            "no_orphan_provider_processes",
        ),
        implementation=f"server/{spec.executable_module.replace('.', '/')}.py".replace("server/zerg/", "server/zerg/"),
        oracle_source="server/zerg/qa/provider_resume_oracles.py",
        oracle_entrypoint="native_resume_assertions",
        executable_module=spec.executable_module,
    )


class PtyProcess:
    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str], recording: Path) -> None:
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (40, 140))
        self.master = master
        self.recording = recording
        self.recording.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.recording.open("ab", buffering=0)
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)

    @property
    def pid(self) -> int:
        return self.process.pid

    def drain(self) -> bytes:
        output = bytearray()
        while True:
            ready, _, _ = select.select([self.master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 64 * 1024)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                raise
            if not chunk:
                break
            self._stream.write(chunk)
            output.extend(chunk)
        return bytes(output)

    def send(self, text: str) -> None:
        os.write(self.master, text.encode())

    def wait(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self.drain()
            time.sleep(0.1)
        self.drain()
        return self.process.poll()

    def settle(self, *, minimum: float = 0.75, quiet: float = 0.35, timeout: float = 5.0) -> bytes:
        """Drain the PTY until the upstream TUI has had time to attach."""

        deadline = time.monotonic() + timeout
        minimum_deadline = time.monotonic() + minimum
        quiet_deadline: float | None = None
        captured = bytearray()
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                captured.extend(self.drain())
                return bytes(captured)
            chunk = self.drain()
            if chunk:
                captured.extend(chunk)
                quiet_deadline = time.monotonic() + quiet
            now = time.monotonic()
            if now >= minimum_deadline and (quiet_deadline is None or now >= quiet_deadline):
                return bytes(captured)
            time.sleep(0.05)
        captured.extend(self.drain())
        return bytes(captured)

    def kill_group(self, sig: int) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, sig)

    def close(self) -> None:
        self.drain()
        self._stream.close()
        try:
            os.close(self.master)
        except OSError:
            pass


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _process_group_dead(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_process_group_dead(pid: int, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_group_dead(pid):
            return True
        time.sleep(0.1)
    return _process_group_dead(pid)


def _pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_pid_dead(pid: int, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_dead(pid):
            return True
        time.sleep(0.1)
    return _pid_dead(pid)


def _signal_pid_if_alive(pid: int, sig: int) -> bool:
    if _pid_dead(pid):
        return False
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    return True


def _provider_process_pid(spec: ProviderSpec, state: dict[str, Any]) -> int:
    field = {"claude": "claude_pid", "cursor": "cursor_pid", "opencode": "pid"}[spec.provider]
    pid = state.get(field)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError(f"{spec.provider} Helm state did not expose positive {field}")
    return pid


def _provider_process_field(spec: ProviderSpec) -> str:
    return {"claude": "claude_pid", "cursor": "cursor_pid", "opencode": "pid"}[spec.provider]


def _endpoint_absence(state: dict[str, Any]) -> dict[str, Any]:
    socket_path = state.get("socket_path")
    if isinstance(socket_path, str) and socket_path:
        return {"kind": "unix_socket", "endpoint": socket_path, "absent": not Path(socket_path).exists()}
    port = state.get("port")
    if isinstance(port, int) and port > 0:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        except OSError:
            return {"kind": "tcp_port", "endpoint": f"127.0.0.1:{port}", "absent": True}
        connection.close()
        return {"kind": "tcp_port", "endpoint": f"127.0.0.1:{port}", "absent": False}
    server_url = state.get("server_url")
    if isinstance(server_url, str) and server_url:
        try:
            urllib.request.urlopen(f"{server_url.rstrip('/')}/global/health", timeout=0.5).close()
        except urllib.error.HTTPError:
            return {"kind": "http", "endpoint": server_url, "absent": False}
        except (OSError, urllib.error.URLError):
            return {"kind": "http", "endpoint": server_url, "absent": True}
        return {"kind": "http", "endpoint": server_url, "absent": False}
    return {"kind": "none", "endpoint": None, "absent": True}


def _cleanup_processes(
    spec: ProviderSpec,
    processes: tuple[PtyProcess | None, ...],
    states: list[dict[str, Any]],
) -> dict[str, Any]:
    forced_pids: list[int] = []
    for process in processes:
        if process is not None and process.process.poll() is None:
            forced_pids.append(process.pid)
            process.kill_group(signal.SIGKILL)
            process.wait(5)
    provider_pids: list[int] = []
    provider_pid_errors: list[dict[str, str]] = []
    for state in states:
        try:
            provider_pids.append(_provider_process_pid(spec, state))
        except RuntimeError as exc:
            provider_pid_errors.append(
                {
                    "session_id": str(state.get("session_id") or "unknown"),
                    "error": str(exc),
                }
            )
    provider_pids = sorted(set(provider_pids))
    forced_provider_pids: list[int] = []
    for pid in provider_pids:
        if _signal_pid_if_alive(pid, signal.SIGKILL):
            forced_provider_pids.append(pid)
    process_receipts = [
        {
            "pid": process.pid,
            "process_exited": process.process.poll() is not None,
            "process_group_dead": _wait_process_group_dead(process.pid),
        }
        for process in processes
        if process is not None
    ]
    provider_process_receipts = [{"pid": pid, "process_dead": _wait_pid_dead(pid)} for pid in provider_pids]
    endpoint_receipts = [_endpoint_absence(state) for state in states]
    orphan_count = sum(not receipt["process_exited"] or not receipt["process_group_dead"] for receipt in process_receipts)
    orphan_count += sum(not receipt["process_dead"] for receipt in provider_process_receipts)
    orphan_count += len(provider_pid_errors)
    endpoints_absent = all(receipt["absent"] for receipt in endpoint_receipts)
    verified = orphan_count == 0 and endpoints_absent
    return {
        "verification": {"verified": verified},
        "verified": verified,
        "orphan_count": orphan_count,
        "processes": process_receipts,
        "provider_processes": provider_process_receipts,
        "provider_pid_errors": provider_pid_errors,
        "forced_cleanup_pids": forced_pids,
        "forced_provider_cleanup_pids": forced_provider_pids,
        "control_endpoints": endpoint_receipts,
        "final_socket_absent": endpoints_absent,
    }


def _artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _state_candidates(spec: ProviderSpec, home: Path) -> list[Path]:
    roots = [home]
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        roots.append(Path(configured).expanduser())
    paths = [path for root in roots for pattern in spec.state_patterns for path in root.glob(pattern)]
    if spec.provider == "opencode":
        paths.extend(path for root in roots for path in root.rglob("*.json") if "opencode-server" in path.parts)
    return sorted(set(paths), key=lambda path: path.stat().st_mtime_ns if path.exists() else 0, reverse=True)


def _normalize_state(spec: ProviderSpec, payload: dict[str, Any], path: Path) -> dict[str, Any] | None:
    session_id = payload.get("session_id") or payload.get("longhouse_session_id")
    provider_thread_id = payload.get("provider_session_id")
    if not isinstance(session_id, str) or not session_id or not isinstance(provider_thread_id, str) or not provider_thread_id:
        return None
    provider = payload.get("provider") or spec.provider
    if provider != spec.provider:
        return None
    return {
        **payload,
        "session_id": session_id,
        "provider_session_id": provider_thread_id,
        "provider": provider,
        "state_path": str(path),
    }


def _wait_state(
    spec: ProviderSpec,
    home: Path,
    *,
    session_id: str | None = None,
    prior_run_id: str | None = None,
    timeout: float = 45,
    process: PtyProcess | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None:
            process.drain()
            if process.process.poll() is not None:
                raise RuntimeError(f"{spec.provider} Helm process exited before publishing state")
        for path in _state_candidates(spec, home):
            try:
                state = _normalize_state(spec, _read_json(path), path)
            except (OSError, json.JSONDecodeError):
                continue
            if state is None or (session_id is not None and state["session_id"] != session_id):
                continue
            if prior_run_id is not None and state.get("run_id") == prior_run_id:
                continue
            provider_pid = state.get(_provider_process_field(spec))
            if (
                all(state.get(field) for field in ("run_id", "connection_id"))
                and isinstance(provider_pid, int)
                and not isinstance(provider_pid, bool)
                and provider_pid > 0
            ):
                return state
        time.sleep(0.2)
    raise RuntimeError(f"{spec.provider} Helm state did not expose the registered run and connection")


def _api_json(api_url: str, token: str, path: str, *, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/agents/{path.lstrip('/')}",
        headers={"X-Agents-Token": token, "Accept": "application/json"},
        data=b"" if method == "POST" else None,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime Host returned a non-object")
    return payload


def _wait_resume_intent(
    spec: ProviderSpec,
    args: argparse.Namespace,
    session_id: str,
    *,
    timeout: float = 45,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_reason = "resume intent was not projected"
    while time.monotonic() < deadline:
        try:
            intent = _api_json(
                args.api_url,
                args.agents_token,
                f"sessions/{session_id}/resume-intent",
                method="POST",
            )
        except (OSError, urllib.error.URLError) as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
            continue
        if intent.get("available") is True:
            return intent
        last_reason = str(intent.get("reason") or "resume intent unavailable")
        time.sleep(0.5)
    raise RuntimeError(f"provider-neutral Resume intent remained unavailable: {last_reason}")


def _command_from_resume_intent(
    spec: ProviderSpec,
    args: argparse.Namespace,
    session_id: str,
    intent: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    expected_argv = [
        "longhouse",
        spec.provider,
        "--cwd",
        str(args.repo_root),
        spec.resume_flag,
        session_id,
    ]
    received_argv = intent.get("argv")
    identity_valid = (
        intent.get("available") is True
        and intent.get("session_id") == session_id
        and intent.get("provider") == spec.provider
        and intent.get("cwd") == str(args.repo_root)
        and intent.get("handoff") == "terminal_command"
        and received_argv == expected_argv
    )
    if not identity_valid:
        raise RuntimeError("provider-neutral Resume intent did not match the exact session, provider, cwd, and native selector")
    selector_index = expected_argv.index(spec.resume_flag)
    overrides = [
        "--url",
        args.api_url,
        "--token",
        args.agents_token,
        spec.binary_flag,
        str(args.provider_bin),
    ]
    if spec.provider == "cursor":
        overrides.extend(("--permission-mode", "auto_approve"))
    command = [str(args.longhouse_cli), *expected_argv[1:selector_index], *overrides, *expected_argv[selector_index:]]
    retained_command = ["<redacted>" if value == args.agents_token else value for value in command]
    receipt = {
        "requested_at": _now(),
        "intent": intent,
        "identity_valid": identity_valid,
        "executed_argv": retained_command,
        "executed_argv_sha256": f"sha256:{hashlib.sha256(json.dumps(command, separators=(',', ':')).encode()).hexdigest()}",
        "factory_overrides": [
            "runtime_host",
            "agents_token",
            "provider_binary",
            *(("permission_mode",) if spec.provider == "cursor" else ()),
        ],
    }
    return command, receipt


def _assistant_contains(value: Any, marker: str) -> bool:
    if isinstance(value, dict):
        role = str(value.get("role") or value.get("type") or "").lower()
        if role == "assistant" and marker in json.dumps(value, ensure_ascii=False):
            return True
        return any(_assistant_contains(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_assistant_contains(item, marker) for item in value)
    return False


def _wait_assistant_marker(api_url: str, token: str, session_id: str, marker: str, *, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = _api_json(api_url, token, f"sessions/{session_id}/tail?limit=100&roles=user,assistant")
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
            continue
        if _assistant_contains(last, marker):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"provider transcript did not retain assistant marker {marker}")


def _control_send(spec: ProviderSpec, args: argparse.Namespace, state: dict[str, Any], process: PtyProcess, text: str) -> dict[str, Any]:
    if spec.provider == "claude":
        command = [str(args.engine), "claude-channel", "send", "--session-id", state["session_id"], "--text", text]
    elif spec.provider == "cursor":
        command = [str(args.engine), "cursor-helm", "send", "--session-id", state["session_id"], "--text", text]
    else:
        if process.process.poll() is not None:
            raise RuntimeError("OpenCode terminal control owner is no longer live")
        process.send(text + "\r")
        return {"method": "provider_tty", "returncode": 0}
    completed = subprocess.run(command, cwd=args.repo_root, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode:
        raise RuntimeError(f"{spec.provider} managed control send failed: {completed.stderr[-1000:]}")
    return {"method": "longhouse_control", "returncode": completed.returncode, "stdout": completed.stdout[-2000:]}


def _stop(spec: ProviderSpec, args: argparse.Namespace, state: dict[str, Any], process: PtyProcess, *, force: bool) -> dict[str, Any]:
    pid = process.pid
    provider_pid = _provider_process_pid(spec, state)
    if force:
        process.kill_group(signal.SIGKILL)
        method = "sigkill_exact_owner_group"
    elif spec.provider == "cursor":
        subprocess.run(
            [str(args.engine), "cursor-helm", "stop", "--session-id", state["session_id"]],
            cwd=args.repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        method = "cursor_helm_stop"
    elif spec.provider == "opencode":
        subprocess.run(
            [str(args.engine), "opencode-bridge", "stop", "--session-id", state["session_id"]],
            cwd=args.repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        method = "opencode_bridge_stop"
    else:
        process.send("\x04")
        method = "claude_terminal_eof"
    exit_code = process.wait(10)
    fallback_signal: str | None = None
    if exit_code is None:
        fallback_signal = "SIGKILL" if force else "SIGTERM"
        process.kill_group(signal.SIGKILL if force else signal.SIGTERM)
        exit_code = process.wait(5)
    group_dead = _wait_process_group_dead(pid)
    provider_force_signal_sent = force and _signal_pid_if_alive(provider_pid, signal.SIGKILL)
    provider_process_dead = _wait_pid_dead(provider_pid)
    dead = process.process.poll() is not None and group_dead and provider_process_dead
    return {
        "method": method,
        "pid": pid,
        "provider_pid": provider_pid,
        "exit_code": exit_code,
        "fallback_signal": fallback_signal,
        "process_group_dead": group_dead,
        "provider_force_signal_sent": provider_force_signal_sent,
        "provider_process_dead": provider_process_dead,
        "dead": dead,
        "clean": dead and not force and fallback_signal is None and exit_code == 0,
    }


def _launch_command(spec: ProviderSpec, args: argparse.Namespace, session_id: str | None) -> list[str]:
    command = [
        str(args.longhouse_cli),
        spec.provider,
        "--cwd",
        str(args.repo_root),
        "--url",
        args.api_url,
        "--token",
        args.agents_token,
        spec.binary_flag,
        str(args.provider_bin),
    ]
    if spec.provider == "cursor":
        command.extend(("--permission-mode", "auto_approve"))
    if session_id is not None:
        command.extend((spec.resume_flag, session_id))
    return command


def _secret_scan(root: Path, secrets: list[str]) -> list[str]:
    found = []
    encoded = [secret.encode() for secret in secrets if secret]
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "result.json":
            continue
        data = path.read_bytes()
        replaced = data
        for secret in encoded:
            replaced = replaced.replace(secret, b"<redacted>")
        if replaced != data:
            path.write_bytes(replaced)
            found.append(path.relative_to(root).as_posix())
    return found


def _isolated_provider_home() -> Path:
    """Require the factory to provide a disposable provider profile."""

    raw_home = os.environ.get("HOME", "").strip()
    home = Path(raw_home)
    if os.environ.get(QUALIFICATION_SANDBOX_ENV) != QUALIFICATION_SANDBOX_PROFILE:
        raise RuntimeError("native Resume producer requires the qualification sandbox")
    if os.environ.get(QUALIFICATION_HOME_ENV) != raw_home:
        raise RuntimeError("native Resume producer HOME is not bound to the qualification sandbox")
    try:
        normal_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        normal_home = None
    if home in {Path("/root"), Path("/home")} or home == normal_home:
        raise RuntimeError("native Resume producer requires an isolated provider HOME")
    if not home.is_absolute() or not home.is_dir():
        raise RuntimeError("native Resume producer HOME is missing or unavailable")
    return home


def run_native_resume(provider: str, args: argparse.Namespace) -> dict[str, Any]:
    spec = SPECS[provider]
    registration = registration_for(provider)
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": _sha256(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)
    home = _isolated_provider_home()
    environment = os.environ.copy()
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    initial: PtyProcess | None = None
    resumed: PtyProcess | None = None
    concurrent: PtyProcess | None = None
    states: list[dict[str, Any]] = []
    final_cleanup: dict[str, Any] = {"verified": False}
    try:
        initial = PtyProcess(
            _launch_command(spec, args, None),
            cwd=args.repo_root,
            env=environment,
            recording=root / "initial.tty",
        )
        initial_state = _wait_state(spec, home, process=initial)
        initial.settle()
        states.append(initial_state)
        _write_json(root / "initial-bridge-state.json", initial_state)
        initial_provider_pid = _provider_process_pid(spec, initial_state)
        seed_marker = f"LONGHOUSE_{provider.upper()}_RESUME_SEED_{uuid.uuid4().hex}"
        _control_send(spec, args, initial_state, initial, f"Reply exactly {seed_marker} and nothing else.")
        initial_tail = _wait_assistant_marker(
            args.api_url, args.agents_token, initial_state["session_id"], seed_marker, timeout=args.live_send_timeout_secs
        )
        _write_json(root / "initial-transcript.jsonl", initial_tail)

        transition = _stop(spec, args, initial_state, initial, force=args.variant == "process_loss")
        _write_json(root / "process-transition-receipt.json", transition)
        stale_marker = f"LONGHOUSE_{provider.upper()}_STALE_{uuid.uuid4().hex}"
        try:
            _control_send(spec, args, initial_state, initial, f"Reply exactly {stale_marker} and nothing else.")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            stale = {"marker": stale_marker, "rejected": True, "error": f"{type(exc).__name__}: {exc}"}
        else:
            stale = {"marker": stale_marker, "rejected": False}
        _write_json(root / "stale-input-receipt.json", stale)

        resume_intent = _wait_resume_intent(spec, args, initial_state["session_id"])
        resumed_command, resume_intent_receipt = _command_from_resume_intent(
            spec,
            args,
            initial_state["session_id"],
            resume_intent,
        )
        _write_json(root / "resume-intent-receipt.json", resume_intent_receipt)
        resumed = PtyProcess(
            resumed_command,
            cwd=args.repo_root,
            env=environment,
            recording=root / "native-resume.tty",
        )
        resumed_state = _wait_state(
            spec,
            home,
            session_id=initial_state["session_id"],
            prior_run_id=str(initial_state["run_id"]),
            process=resumed,
        )
        resumed.settle()
        states.append(resumed_state)
        _write_json(root / "resumed-bridge-state.json", resumed_state)
        resumed_provider_pid = _provider_process_pid(spec, resumed_state)
        post_marker = f"LONGHOUSE_{provider.upper()}_RESUME_POST_{uuid.uuid4().hex}"
        post_send = _control_send(spec, args, resumed_state, resumed, f"Reply exactly {post_marker} and nothing else.")
        _write_json(root / "post-resume-send.json", post_send)
        resumed_tail = _wait_assistant_marker(
            args.api_url, args.agents_token, resumed_state["session_id"], post_marker, timeout=args.live_send_timeout_secs
        )
        _write_json(root / "resumed-transcript.jsonl", resumed_tail)
        post_resume_marker_observed = _assistant_contains(resumed_tail, post_marker)
        stale_generation_dispatched = _assistant_contains(resumed_tail, stale_marker)

        concurrent = PtyProcess(
            list(resumed_command),
            cwd=args.repo_root,
            env=environment,
            recording=root / "concurrent-resume-attempt.tty",
        )
        concurrent_exit = concurrent.wait(10)
        concurrent_refused = concurrent_exit not in {None, 0} and resumed.process.poll() is None
        if concurrent_exit is None:
            concurrent.kill_group(signal.SIGKILL)
            concurrent.wait(5)
        concurrent_receipt = {
            "rejected": concurrent_refused,
            "exit_code": concurrent_exit,
            "active_owner_preserved": resumed.process.poll() is None,
        }
        _write_json(root / "concurrent-resume-receipt.json", concurrent_receipt)

        _stop(spec, args, resumed_state, resumed, force=False)
        final_cleanup = _cleanup_processes(spec, (initial, resumed, concurrent), states)
        _write_json(root / "cleanup-receipt.json", final_cleanup)
        redacted = _secret_scan(
            root,
            [
                args.agents_token,
                os.environ.get("ANTHROPIC_API_KEY", ""),
                os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
                os.environ.get("CURSOR_API_KEY", ""),
                os.environ.get("OPENROUTER_API_KEY", ""),
            ],
        )
        observation = {
            "variant": args.variant,
            "same_longhouse_session": resumed_state["session_id"] == initial_state["session_id"],
            "same_provider_thread": resumed_state["provider_session_id"] == initial_state["provider_session_id"],
            "new_run": resumed_state["run_id"] != initial_state["run_id"],
            "new_connection": resumed_state["connection_id"] != initial_state["connection_id"],
            "new_app_server_process": resumed_provider_pid != initial_provider_pid,
            "initial_provider_pid": initial_provider_pid,
            "resumed_provider_pid": resumed_provider_pid,
            "provider_neutral_resume_intent": resume_intent_receipt["identity_valid"] is True,
            "native_resume_command": (
                spec.resume_flag in resumed_command
                and resumed_command[resumed_command.index(spec.resume_flag) + 1] == initial_state["session_id"]
            ),
            "bridge_subscribed": all(
                resumed_state.get(field) for field in ("session_id", "provider_session_id", "run_id", "connection_id")
            ),
            "post_resume_provider_activity": post_resume_marker_observed,
            "post_resume_marker_in_assistant_transcript": post_resume_marker_observed,
            "stale_input_rejected": stale["rejected"] is True,
            "stale_generation_dispatched": stale_generation_dispatched,
            "concurrent_resume_refused": concurrent_refused,
            "artifact_secret_scan_passed": not redacted,
            "clean_stop_verified": args.variant == "clean_exit" and transition["clean"],
            "old_bridge_process_dead": args.variant == "process_loss" and transition["dead"],
            "old_app_server_process_dead": args.variant == "process_loss" and transition["provider_process_dead"],
            "replacement_started_after_process_loss": args.variant == "process_loss" and resumed_state["run_id"] != initial_state["run_id"],
            "final_cleanup_verified": final_cleanup["verified"],
            "final_socket_absent": final_cleanup["final_socket_absent"],
            "orphan_count": final_cleanup["orphan_count"],
        }
        assertions = native_resume_assertions(args.variant, observation)
        result = {
            "schema_version": 1,
            "artifact_kind": "direct_native_resume_result",
            "producer": registration.to_dict(),
            "provider": provider,
            "variant": args.variant,
            "scenario_id": registration.scenario_id,
            "scenario_revision": registration.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "pass" if assertions["native_provider_resume_proven"] else "fail",
            "observation": observation,
            "assertions": assertions,
            "session_id": initial_state["session_id"],
            "provider_thread_id": initial_state["provider_session_id"],
            "provider_binary": provider_receipt,
            "seed_marker": seed_marker,
            "post_resume_marker": post_marker,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain the exact causal failure
        final_cleanup = _cleanup_processes(spec, (initial, resumed, concurrent), states)
        _write_json(root / "cleanup-receipt.json", final_cleanup)
        redacted = _secret_scan(root, [args.agents_token])
        failure = {
            "schema_version": 1,
            "artifact_kind": "direct_native_resume_result",
            "producer": registration.to_dict(),
            "provider": provider,
            "variant": args.variant,
            "scenario_id": registration.scenario_id,
            "scenario_revision": registration.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "direct_native_resume_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "redacted_secret_files": redacted,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
        return failure
    finally:
        if not final_cleanup.get("verified"):
            final_cleanup = _cleanup_processes(spec, (initial, resumed, concurrent), states)
            _write_json(root / "cleanup-receipt.json", final_cleanup)
        for process in (concurrent, resumed, initial):
            if process is None:
                continue
            process.close()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--variant", required=True, choices=("clean_exit", "process_loss"))
    value.add_argument("--evidence-root", required=True, type=Path)
    value.add_argument("--repo-root", required=True, type=Path)
    value.add_argument("--engine", required=True, type=Path)
    value.add_argument("--longhouse-cli", required=True, type=Path)
    value.add_argument("--provider-bin", required=True, type=Path)
    value.add_argument("--live-send-timeout-secs", type=int, default=180)
    return value


def main_for(provider: str, argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(registration_for(provider).to_dict(), indent=2, sort_keys=True))
        return 0
    args = parser().parse_args(arguments)
    args.api_url = os.environ.get("CODEX_API_URL", "")
    args.agents_token = os.environ.get("CODEX_AGENTS_TOKEN", "")
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    for path, label in (
        (args.engine, "longhouse_engine"),
        (args.longhouse_cli, "longhouse_cli"),
        (args.provider_bin, f"{provider}_binary"),
    ):
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": f"{label}_missing"}))
            return 2
    result = run_native_resume(provider, args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


__all__ = ["SPECS", "main_for", "registration_for", "run_native_resume"]
