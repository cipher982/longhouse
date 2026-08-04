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
import re
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
_ANSI_CONTROL_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_]|.)")
_RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"
_EVIDENCE_SECRET_KEY_RE = re.compile(r"(?:^|_)(?:token|secret|password|api_key|access_key|authorization)$")


def _qualification_secrets(environment: dict[str, str], agents_token: str) -> tuple[str, ...]:
    """Return every credential value that must not survive in evidence."""

    names = (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_API_KEY",
        "CURSOR_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "PROVIDER_FACTORY_PUBLISH_TOKEN",
        "PROVIDER_FACTORY_TRIAGE_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    return tuple(dict.fromkeys(value for value in (agents_token, *(environment.get(name, "") for name in names)) if value))


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    producer_id: str
    executable_module: str
    binary_flag: str
    resume_flag: str
    credential_binding_id: str
    state_patterns: tuple[str, ...]


class TranscriptShipper:
    """Own the disposable Machine Agent used by a live provider qualification.

    A managed provider bridge can create live control state without creating a
    transcript/archive projection.  The real machine path has a second local
    process—the engine connect daemon—that watches provider files and ships
    them.  Qualification must run that process too or hosted readiness checks
    are testing an artificial half-stack.
    """

    def __init__(
        self,
        process: subprocess.Popen[Any],
        log_stream: Any,
        receipt: dict[str, Any],
        *,
        engine: Path,
        repo_root: Path,
        api_url: str,
        machine_name: str,
        db_path: Path,
        engine_environment: dict[str, str],
        evidence_root: Path,
        redaction_secrets: tuple[str, ...],
        connect_command: list[str],
    ) -> None:
        self.process = process
        self.log_stream = log_stream
        self.receipt = receipt
        self.engine = engine
        self.repo_root = repo_root
        self.api_url = api_url
        self.machine_name = machine_name
        self.db_path = db_path
        self.engine_environment = engine_environment
        self.evidence_root = evidence_root
        self.redaction_secrets = tuple(secret for secret in redaction_secrets if secret)
        self.connect_command = tuple(connect_command)
        self._stopped = False

    def _redact(self, value: str) -> str:
        for secret in self.redaction_secrets:
            value = value.replace(secret, "<redacted>")
        return value

    def flush(self, label: str) -> dict[str, Any]:
        """Force a bounded scan before a hosted projection assertion."""

        # A fresh engine DB creates a fresh storage-v2 source epoch. The
        # Runtime Host only accepts epochs registered by the long-lived
        # daemon, so pause that daemon and reuse its enrolled DB for the
        # one-shot scan. Restart it before returning to the provider probe.
        command = [
            str(self.engine),
            "ship",
            "--url",
            self.api_url,
            "--db",
            str(self.db_path),
            "--machine-name",
            self.machine_name,
            "--json",
        ]
        daemon_was_live = self.process.poll() is None
        daemon_restart_error: str | None = None
        if daemon_was_live:
            self._terminate_daemon()
        try:
            completed = subprocess.run(
                command,
                cwd=self.repo_root,
                env=self.engine_environment,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            result = {
                "status": "fail",
                "label": label,
                "exit_code": None,
                "timed_out": True,
                "stdout_sha256": hashlib.sha256(str(exc.stdout or "").encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(str(exc.stderr or "").encode()).hexdigest(),
            }
        except OSError as exc:
            result = {
                "status": "fail",
                "label": label,
                "exit_code": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            stdout = self._redact(completed.stdout or "")
            stderr = self._redact(completed.stderr or "")
            try:
                summary = json.loads(completed.stdout)
            except (TypeError, json.JSONDecodeError):
                summary = {}
            if not isinstance(summary, dict):
                summary = {}
            result = {
                "status": "pass" if completed.returncode == 0 else "fail",
                "label": label,
                "exit_code": completed.returncode,
                "protocol": summary.get("protocol"),
                "files_scanned": summary.get("files_scanned"),
                "files_shipped": summary.get("files_shipped"),
                "events_shipped": summary.get("events_shipped"),
                "spool_replayed": summary.get("spool_replayed"),
                "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
            }
            log_path = self.evidence_root / f"transcript-flush-{label}.log"
            log_path.write_text(f"stdout:\n{stdout}\nstderr:\n{stderr}\n", encoding="utf-8")
            result["log_path"] = str(log_path)
        if daemon_was_live:
            try:
                self._restart_daemon()
            except Exception as exc:  # noqa: BLE001 - retain restart evidence in the receipt
                daemon_restart_error = f"{type(exc).__name__}: {exc}"
                result["status"] = "fail"
        result["daemon_paused"] = daemon_was_live
        result["daemon_restarted"] = daemon_was_live and daemon_restart_error is None
        if daemon_restart_error:
            result["daemon_restart_error"] = daemon_restart_error
        return result

    def _terminate_daemon(self) -> str | None:
        signal_sent: str | None = None
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                signal_sent = "SIGTERM"
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    signal_sent = "SIGKILL"
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        self.log_stream.flush()
        return signal_sent

    def _restart_daemon(self) -> None:
        socket_path = Path(str(self.receipt["socket_path"]))
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        self.process = subprocess.Popen(
            list(self.connect_command),
            cwd=self.repo_root,
            env=self.engine_environment,
            stdin=subprocess.DEVNULL,
            stdout=self.log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if socket_path.exists():
                return
            if self.process.poll() is not None:
                raise RuntimeError(f"Longhouse transcript shipper exited during restart (exit_code={self.process.returncode})")
            time.sleep(0.1)
        raise RuntimeError("Longhouse transcript shipper did not become ready after flush")

    def stop(self) -> dict[str, Any]:
        if self._stopped:
            return self.receipt
        self._stopped = True
        signal_sent = self._terminate_daemon()
        self.receipt.update(
            {
                "stopped": True,
                "signal": signal_sent,
                "exit_code": self.process.returncode,
                "process_dead": self.process.poll() is not None,
                "process_group_dead": _wait_process_group_dead(self.process.pid),
            }
        )
        try:
            self.log_stream.close()
        except OSError:
            pass
        return self.receipt


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
            "transcript_shipper_receipt",
            "resume_intent_receipt",
            "initial_bridge_state",
            "initial_seed_send",
            "initial_transcript",
            "initial_transcript_ship_receipt",
            "native_resume_terminal_recording",
            "resumed_bridge_state",
            "resumed_transcript",
            "post_resume_response_correlation",
            "post_resume_transcript_ship_receipt",
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
        self.claude_permission_acceptance_sent = False
        self.claude_development_channel_acceptance_sent = False
        self.claude_development_channel_prompt_seen_at: float | None = None
        self.cursor_workspace_trust_sent = False
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


def _provision_transcript_roots(home: Path, environment: dict[str, str]) -> None:
    """Create the provider roots the real engine discovers at startup."""

    roots = [
        home / ".codex" / "sessions",
        home / ".local" / "share" / "opencode",
        home / ".cursor" / "chats",
        home / ".longhouse" / "agent" / "cursor-acp-source",
    ]
    configured_claude_dir = str(environment.get("CLAUDE_CONFIG_DIR") or "").strip()
    roots.append(Path(configured_claude_dir) / "projects" if configured_claude_dir else home / ".claude" / "projects")
    for path in roots:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _start_transcript_shipper(
    provider: str,
    args: argparse.Namespace,
    *,
    home: Path,
    environment: dict[str, str],
    evidence_root: Path,
    longhouse_home: Path | None = None,
) -> TranscriptShipper:
    """Start the same file-watching Machine Agent used outside the factory."""

    _provision_transcript_roots(home, environment)
    engine_longhouse_home = longhouse_home or (home / ".longhouse")
    machine_dir = engine_longhouse_home / "machine"
    agent_dir = engine_longhouse_home / "agent"
    machine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    capabilities = _api_json(args.api_url, args.agents_token, "storage/v2/capabilities")
    machine_id = str(capabilities.get("machine_id") or "").strip()
    if not machine_id:
        raise RuntimeError("Runtime Host storage capabilities did not return the authenticated machine identity")
    _write_json(
        machine_dir / "state.json",
        {"runtime_url": args.api_url, "machine_name": machine_id},
    )
    token_path = machine_dir / "device-token"
    token_path.write_text(args.agents_token.strip() + "\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    db_path = agent_dir / "longhouse-shipper.db"
    socket_path = agent_dir / "transcript-wake.sock"
    log_path = evidence_root / "transcript-shipper.log"
    log_stream = log_path.open("w", encoding="utf-8")
    engine_environment = dict(environment)
    engine_environment["HOME"] = str(home)
    engine_environment["LONGHOUSE_HOME"] = str(engine_longhouse_home)
    engine_environment.setdefault("RUST_LOG", "longhouse_engine=info")
    environment["LONGHOUSE_HOME"] = str(engine_longhouse_home)
    if provider != "claude":
        # A Codex/OpenCode/Cursor qualification may inherit the factory's
        # staged Claude profile. Do not let the shipper use that profile as a
        # second machine source.
        engine_environment.pop("CLAUDE_CONFIG_DIR", None)
    command = [
        str(args.engine),
        "connect",
        "--url",
        args.api_url,
        "--db",
        str(db_path),
        "--machine-name",
        machine_id,
        "--fallback-scan-secs",
        "1",
        "--spool-replay-secs",
        "1",
        "--archive-repair-mode",
        "drain",
        "--log-dir",
        str(evidence_root / "engine-logs"),
    ]
    process = subprocess.Popen(
        command,
        cwd=args.repo_root,
        env=engine_environment,
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if socket_path.exists():
            receipt = {
                "status": "started",
                "provider": provider,
                "engine_path": str(args.engine),
                "pid": process.pid,
                "machine_name": machine_id,
                "socket_path": str(socket_path),
                "db_path": str(db_path),
                "log_dir": str(evidence_root / "engine-logs"),
                "ready": True,
            }
            return TranscriptShipper(
                process,
                log_stream,
                receipt,
                engine=args.engine,
                repo_root=args.repo_root,
                api_url=args.api_url,
                machine_name=machine_id,
                db_path=db_path,
                engine_environment=engine_environment,
                evidence_root=evidence_root,
                redaction_secrets=(*_qualification_secrets(environment, args.agents_token),),
                connect_command=command,
            )
        if process.poll() is not None:
            log_stream.flush()
            try:
                detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                detail = ""
            log_stream.close()
            raise RuntimeError(
                "Longhouse transcript shipper exited before readiness "
                f"(provider={provider}, exit_code={process.returncode}, log={detail!r})"
            )
        time.sleep(0.1)
    process.kill()
    process.wait(timeout=5)
    log_stream.close()
    raise RuntimeError(f"Longhouse transcript shipper did not become ready for {provider}")


def _signal_pid_if_alive(pid: int, sig: int) -> bool:
    if _pid_dead(pid):
        return False
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    return True


def _opencode_attach_pids(states: list[dict[str, Any]]) -> list[int]:
    """Find only attach processes belonging to these recorded sessions.

    The stock OpenCode TUI can outlive the Longhouse facade that launched it
    when its localhost server disappears.  The bridge state records the
    provider session, so a failed qualification can clean those exact attach
    processes without scanning or signaling unrelated provider work.
    """

    identities = {(str(state.get("session_id") or ""), str(state.get("provider_session_id") or "")) for state in states}
    identities.discard(("", ""))
    if not identities:
        return []
    pids: set[int] = set()
    proc_root = Path("/proc")
    proc_entries = proc_root.iterdir() if proc_root.is_dir() else ()
    for proc in proc_entries:
        if not proc.name.isdigit():
            continue
        try:
            command = proc.joinpath("cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "opencode-bridge attach" in command:
            if any(session_id in command for session_id, _provider_session_id in identities):
                pids.add(int(proc.name))
        elif " attach http://127.0.0.1:" in command:
            if any(provider_session_id in command for _session_id, provider_session_id in identities):
                pids.add(int(proc.name))
    return sorted(pids)


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
    attach_pids = _opencode_attach_pids(states) if spec.provider == "opencode" else []
    forced_attach_pids: list[int] = []
    for pid in attach_pids:
        if _signal_pid_if_alive(pid, signal.SIGKILL):
            forced_attach_pids.append(pid)
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
    attach_process_receipts = [{"pid": pid, "process_dead": _wait_pid_dead(pid)} for pid in attach_pids]
    endpoint_receipts = [_endpoint_absence(state) for state in states]
    orphan_count = sum(not receipt["process_exited"] or not receipt["process_group_dead"] for receipt in process_receipts)
    orphan_count += sum(not receipt["process_dead"] for receipt in provider_process_receipts)
    orphan_count += sum(not receipt["process_dead"] for receipt in attach_process_receipts)
    orphan_count += len(provider_pid_errors)
    endpoints_absent = all(receipt["absent"] for receipt in endpoint_receipts)
    verified = orphan_count == 0 and endpoints_absent
    return {
        "verification": {"verified": verified},
        "verified": verified,
        "orphan_count": orphan_count,
        "processes": process_receipts,
        "provider_processes": provider_process_receipts,
        "attach_processes": attach_process_receipts,
        "provider_pid_errors": provider_pid_errors,
        "forced_cleanup_pids": forced_pids,
        "forced_provider_cleanup_pids": forced_provider_pids,
        "forced_attach_cleanup_pids": forced_attach_pids,
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


def _terminal_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return _ANSI_CONTROL_RE.sub("", raw.decode("utf-8", "replace"))


def _accept_claude_permission_prompt(process: PtyProcess) -> None:
    """Accept Claude's first-run bypass-permissions acknowledgement.

    The managed facade deliberately launches Claude in the sandboxed bypass
    mode used by the qualification.  Recent Claude builds add a native TUI
    acknowledgement for that mode before they publish the channel state.  It
    is a provider startup control record, not a user turn, so the live probe
    must acknowledge it before looking for the registered session.
    """

    if process.claude_permission_acceptance_sent:
        return
    compact = re.sub(r"\s+", "", _terminal_text(process.recording))
    if "1.No,exit" in compact and "2.Yes,Iaccept" in compact:
        # The provider renders numbered choices and accepts the visible
        # choice key directly. This avoids depending on whether its selector
        # currently installed normal- or application-cursor mode.
        process.send("2\r")
        process.claude_permission_acceptance_sent = True


def _accept_claude_development_channel_prompt(process: PtyProcess) -> None:
    """Select Claude's explicit local-development channel acknowledgement."""

    if getattr(process, "claude_development_channel_acceptance_sent", False):
        return
    compact = re.sub(r"\s+", "", _terminal_text(process.recording)).lower()
    # The loading label is cursor-addressed and Claude's PTY redraw can omit
    # characters when ANSI controls are stripped. The visible option text is
    # the stable selector contract; require it plus Exit, not the decorative
    # loading label.
    if "iamusingthisforlocaldevelopment" in compact and "exit" in compact:
        # Claude renders an unnumbered selector with the local-development
        # acknowledgement before Exit. The provider docs say the first option
        # is selected by default. Let the screen finish entering its input
        # mode before accepting it; sending an arrow during the render race
        # leaves the provider at this prompt without registering the channel.
        seen_at = getattr(process, "claude_development_channel_prompt_seen_at", None)
        now = time.monotonic()
        if seen_at is None:
            process.claude_development_channel_prompt_seen_at = now
            return
        if now - seen_at < 1.0:
            return
        process.send("\r")
        process.claude_development_channel_acceptance_sent = True


def _accept_cursor_workspace_trust(process: PtyProcess) -> None:
    """Accept Cursor's provider-owned first-run workspace trust gate once."""

    if getattr(process, "cursor_workspace_trust_sent", False):
        return
    compact = re.sub(r"\s+", "", _terminal_text(process.recording)).lower()
    if "workspacetrustrequired" in compact and "[a]trustthisworkspace" in compact:
        process.send("a")
        process.cursor_workspace_trust_sent = True


def _wait_cursor_tui_ready(process: PtyProcess, recording: Path, *, timeout: float = 30.0) -> None:
    """Give Cursor time to render and accept its native workspace gate.

    Cursor writes its managed lease before the interactive trust screen is
    necessarily rendered. Waiting only for the lease, or doing one immediate
    prompt check after a short PTY settle, can send the first qualification
    message into a still-blocked TUI. Keep draining the real PTY until the
    prompt is handled or the bounded startup window expires.
    """

    del recording  # The process owns the append-only PTY recording.
    deadline = time.monotonic() + timeout
    accepted_at: float | None = None
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("cursor Helm process exited before its TUI became ready")
        _accept_cursor_workspace_trust(process)
        if getattr(process, "cursor_workspace_trust_sent", False):
            accepted_at = accepted_at or time.monotonic()
            if time.monotonic() - accepted_at >= 0.75:
                return
        time.sleep(0.1)

    # Some provider builds do not render a trust gate for an already-known
    # workspace. The bounded wait still gave late gates a chance to appear;
    # the subsequent native marker is the readiness assertion.


def _wait_claude_tui_ready(process: PtyProcess, recording: Path, *, timeout: float = 30.0) -> None:
    """Wait for Claude's actual input prompt after its native state appears.

    Claude can publish the Longhouse channel state while its TUI is still
    finishing the bypass-permissions acknowledgement and drawing the first
    input prompt. Sending the seed as soon as the state file exists can then
    be consumed by that startup control surface instead of becoming a real
    provider turn. The visible ``❯ Try ...`` prompt is the provider-owned
    readiness boundary for terminal input.
    """

    del recording  # The process owns the append-only PTY recording.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("Claude Helm process exited before its TUI became ready")
        terminal = _terminal_text(process.recording)
        if re.search(r"(?im)(?:^|\n)\s*[❯>]\s*Try\b", terminal):
            process.settle()
            return
        time.sleep(0.1)
    raise RuntimeError("Claude TUI did not publish its input prompt")


def _state_candidate_diagnostics(spec: ProviderSpec, home: Path) -> list[dict[str, Any]]:
    """Retain provider-state identity without copying private state fields."""

    rows: list[dict[str, Any]] = []
    for path in _state_candidates(spec, home):
        try:
            payload = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        normalized = _normalize_state(spec, payload, path)
        if normalized is None:
            continue
        provider_pid_field = _provider_process_field(spec)
        rows.append(
            {
                "path": str(path),
                "provider": spec.provider,
                "session_id": normalized.get("session_id"),
                "provider_session_id": normalized.get("provider_session_id"),
                "run_id": normalized.get("run_id"),
                "connection_id": normalized.get("connection_id"),
                provider_pid_field: normalized.get(provider_pid_field),
                "keys": sorted(str(key) for key in payload),
            }
        )
    return rows


def _wait_opencode_tui_ready(process: PtyProcess, recording: Path, *, timeout: float = 20.0) -> None:
    """Wait for the attached TUI, not merely the localhost bridge, to accept input."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("opencode Helm process exited before its TUI became ready")
        compact = re.sub(r"\s+", "", _terminal_text(recording)).lower()
        if "opencode" in compact and "connected" in compact:
            return
        time.sleep(0.1)
    raise RuntimeError("opencode TUI did not publish its connected state")


def _prepare_claude_profile(
    *,
    binary: Path,
    home: Path,
    workspace: Path,
    environment: dict[str, str],
    recording: Path,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Complete Claude's real first-run onboarding in the disposable profile.

    Claude persists this setup outside the Longhouse channel state. Launching
    the managed facade against a fresh profile otherwise leaves it at the
    theme/API-key/trust screens and no native channel state can be observed.
    """

    configured = str(environment.get("CLAUDE_CONFIG_DIR") or "").strip()
    config_dir = Path(configured) if configured else home / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
    bootstrap_environment = dict(environment)
    bootstrap_environment["HOME"] = str(home)
    process = PtyProcess(
        [str(binary), "--name", "Longhouse qualification bootstrap", "--permission-mode", "dontAsk"],
        cwd=workspace,
        env=bootstrap_environment,
        recording=recording,
    )
    theme_attempts = 0
    api_key_attempts = 0
    security_notes_attempts = 0
    confirmed_trust = False
    started = time.monotonic()
    try:
        deadline = started + timeout
        while time.monotonic() < deadline:
            process.drain()
            if process.process.poll() is not None:
                raise RuntimeError("Claude onboarding process exited before profile completion")
            _accept_claude_permission_prompt(process)
            compact = re.sub(r"\s+", "", _terminal_text(recording))
            if "ClaudeCode" in compact and "Welcomeback!" in compact:
                process.send("\x04")
                process.wait(10)
                return {
                    "status": "pass",
                    "profile": "isolated_disposable",
                    "config_dir": str(config_dir),
                    "completion_signal": "main_tui",
                    "has_completed_onboarding": False,
                    "theme_attempts": theme_attempts,
                    "api_key_attempts": api_key_attempts,
                    "security_notes_attempts": security_notes_attempts,
                    "trust_confirmed": confirmed_trust,
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            if theme_attempts == 0 and "Choosethetextstyle" in compact:
                process.send("\r")
                theme_attempts += 1
            elif "DetectedacustomAPIkey" in compact and api_key_attempts == 0:
                process.send("\x1b[A")
                api_key_attempts = 1
            elif "DetectedacustomAPIkey" in compact and api_key_attempts == 1:
                process.send("\r")
                api_key_attempts = 2
            elif security_notes_attempts == 0 and ("Securitynotes" in compact or "PressEnte" in compact):
                process.send("\r")
                security_notes_attempts += 1
            elif not confirmed_trust and "Yes,Itrustthisfolder" in compact:
                process.send("\r")
                confirmed_trust = True
            else:
                try:
                    profile = json.loads((config_dir / ".claude.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    profile = {}
                approved = profile.get("customApiKeyResponses") if isinstance(profile, dict) else None
                if (
                    isinstance(profile, dict)
                    and profile.get("hasCompletedOnboarding") is True
                    and isinstance(approved, dict)
                    and bool(approved.get("approved"))
                ):
                    process.send("\x04")
                    process.wait(10)
                    return {
                        "status": "pass",
                        "profile": "isolated_disposable",
                        "config_dir": str(config_dir),
                        "has_completed_onboarding": True,
                        "theme_attempts": theme_attempts,
                        "api_key_attempts": api_key_attempts,
                        "security_notes_attempts": security_notes_attempts,
                        "trust_confirmed": confirmed_trust,
                        "duration_seconds": round(time.monotonic() - started, 3),
                    }
            time.sleep(0.1)
        raise RuntimeError(
            "Claude onboarding did not complete "
            f"(theme={theme_attempts}, api_key={api_key_attempts}, security={security_notes_attempts}, trust={confirmed_trust})"
        )
    finally:
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.wait(5)
        process.close()


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


def _redact_state_for_evidence(value: Any) -> Any:
    """Keep provider identity state useful without retaining bridge secrets."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if _EVIDENCE_SECRET_KEY_RE.search(normalized) or normalized.endswith(("_token", "_secret", "_password", "_api_key")):
                redacted[str(key)] = "<redacted>"
            else:
                redacted[str(key)] = _redact_state_for_evidence(item)
        return redacted
    if isinstance(value, list):
        return [_redact_state_for_evidence(item) for item in value]
    return value


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
                diagnostics = _state_candidate_diagnostics(spec, home)
                raise RuntimeError(
                    f"{spec.provider} Helm process exited before publishing state; candidates={json.dumps(diagnostics, sort_keys=True)}"
                )
            if spec.provider == "claude":
                _accept_claude_permission_prompt(process)
                _accept_claude_development_channel_prompt(process)
            elif spec.provider == "cursor":
                _accept_cursor_workspace_trust(process)
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
    diagnostics = _state_candidate_diagnostics(spec, home)
    raise RuntimeError(
        f"{spec.provider} Helm state did not expose the registered run and connection; candidates={json.dumps(diagnostics, sort_keys=True)}"
    )


class _RuntimeHostHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Runtime Host HTTP {status}: {detail}")


def _api_json(api_url: str, token: str, path: str, *, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/agents/{path.lstrip('/')}",
        headers={
            "X-Agents-Token": token,
            "Accept": "application/json",
            "User-Agent": _RUNTIME_HOST_USER_AGENT,
        },
        data=b"" if method == "POST" else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            raw_detail = exc.read(4096).decode("utf-8", "replace")
        except OSError:
            raw_detail = ""
        try:
            parsed_detail = json.loads(raw_detail)
        except json.JSONDecodeError:
            parsed_detail = raw_detail[:1000]
        if isinstance(parsed_detail, dict):
            detail = str(parsed_detail.get("detail") or parsed_detail)[:1000]
        else:
            detail = str(parsed_detail)[:1000]
        raise _RuntimeHostHTTPError(exc.code, detail) from exc
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
        except _RuntimeHostHTTPError as exc:
            if exc.status in {401, 403}:
                raise
            last_reason = str(exc)
            time.sleep(0.5)
            continue
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
    *,
    use_credential_files: bool = False,
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
    overrides = ["--url", args.api_url]
    if not use_credential_files:
        overrides.extend(("--token", args.agents_token))
    overrides.extend((spec.binary_flag, str(args.provider_bin)))
    if spec.provider == "cursor":
        overrides.extend(("--permission-mode", "auto_approve"))
    command = [str(args.longhouse_cli), *expected_argv[1:selector_index], *overrides, *expected_argv[selector_index:]]
    if spec.provider == "cursor":
        cursor_model = os.environ.get("CURSOR_MODEL", "").strip()
        if cursor_model:
            command.extend(("--", "--model", cursor_model))
    retained_command = ["<redacted>" if value == args.agents_token else value for value in command]
    receipt = {
        "requested_at": _now(),
        "intent": intent,
        "identity_valid": identity_valid,
        "executed_argv": retained_command,
        "executed_argv_sha256": f"sha256:{hashlib.sha256(json.dumps(command, separators=(',', ':')).encode()).hexdigest()}",
        "factory_overrides": [
            "runtime_host",
            "provider_binary",
            *(("permission_mode",) if spec.provider == "cursor" else ()),
            *(("cursor_model",) if spec.provider == "cursor" and os.environ.get("CURSOR_MODEL", "").strip() else ()),
        ],
        "credential_source": "disposable_machine_file" if use_credential_files else "argv_token",
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


def _assistant_event_digests(value: Any) -> set[str]:
    """Identify top-level assistant events without retaining their content."""

    if isinstance(value, dict):
        role = str(value.get("role") or value.get("type") or "").lower()
        if role == "assistant":
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
            return {hashlib.sha256(encoded).hexdigest()}
        found: set[str] = set()
        for item in value.values():
            found.update(_assistant_event_digests(item))
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found.update(_assistant_event_digests(item))
        return found
    return set()


def _wait_assistant_marker(api_url: str, token: str, session_id: str, marker: str, *, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = _api_json(api_url, token, f"sessions/{session_id}/tail?limit=100&roles=user,assistant")
        except _RuntimeHostHTTPError as exc:
            if exc.status in {401, 403}:
                raise
            # Session projection can lag the local managed state by a short
            # interval immediately after launch or resume.  Keep polling for
            # transient application errors while preserving auth failures.
            time.sleep(0.5)
            continue
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
            continue
        if _assistant_contains(last, marker):
            return last
        time.sleep(0.5)
    raise RuntimeError(f"provider transcript did not retain assistant marker {marker}")


def _wait_assistant_response_after_marker(
    api_url: str,
    token: str,
    session_id: str,
    marker: str,
    *,
    prior_assistant_event_digests: set[str],
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prove a native control record caused a new provider response.

    Claude's development-channel records are stored as user events, and its
    safety behavior may refuse to repeat a token received from that peer
    channel. Correlate the native marker with a *new* assistant event instead
    of requiring the provider to echo the marker in assistant content.
    """

    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    marker_observed = False
    observed_assistant_event_digests: set[str] = set()
    while time.monotonic() < deadline:
        try:
            last = _api_json(api_url, token, f"sessions/{session_id}/tail?limit=100&roles=user,assistant")
        except _RuntimeHostHTTPError as exc:
            if exc.status in {401, 403}:
                raise
            time.sleep(0.5)
            continue
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
            continue
        marker_observed = marker in json.dumps(last, ensure_ascii=False)
        observed_assistant_event_digests = _assistant_event_digests(last)
        new_assistant_events = observed_assistant_event_digests - prior_assistant_event_digests
        if marker_observed and new_assistant_events:
            return last, {
                "method": "transcript_marker_then_new_assistant_event",
                "marker_observed_in_transcript": marker_observed,
                "prior_assistant_events": len(prior_assistant_event_digests),
                "observed_assistant_events": len(observed_assistant_event_digests),
                "new_assistant_events": len(new_assistant_events),
                "timed_out": False,
            }
        time.sleep(0.5)
    return last, {
        "method": "transcript_marker_then_new_assistant_event",
        "marker_observed_in_transcript": marker_observed,
        "prior_assistant_events": len(prior_assistant_event_digests),
        "observed_assistant_events": len(observed_assistant_event_digests),
        "new_assistant_events": len(observed_assistant_event_digests - prior_assistant_event_digests),
        "timed_out": True,
    }


def _wait_cursor_idle(
    state: dict[str, Any],
    environment: dict[str, str],
    *,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Wait for the provider-owned Cursor hook to publish an idle phase."""

    longhouse_home = str(environment.get("LONGHOUSE_HOME") or "").strip()
    if not longhouse_home:
        raise RuntimeError("Cursor Helm qualification has no explicit Longhouse home")
    path = Path(longhouse_home) / "managed-local" / "cursor-helm" / f"{state['session_id']}.phase.json"
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            payload = _read_json(path)
        except (OSError, json.JSONDecodeError):
            payload = {}
        last = payload
        if (
            payload.get("session_id") == state.get("session_id")
            and payload.get("conversation_id") == state.get("provider_session_id")
            and payload.get("phase") == "idle"
        ):
            return payload
        time.sleep(0.25)
    raise RuntimeError(
        "Cursor native hooks did not publish idle phase "
        f"(path={path}, last_phase={last.get('phase')!r}, last_conversation={last.get('conversation_id')!r})"
    )


def _control_send(
    spec: ProviderSpec,
    args: argparse.Namespace,
    state: dict[str, Any],
    process: PtyProcess,
    text: str,
    *,
    initial: bool = False,
) -> dict[str, Any]:
    if initial and spec.provider == "cursor":
        if process.process.poll() is not None:
            raise RuntimeError(f"{spec.provider} terminal control owner is no longer live")
        process.send(text + "\r")
        return {"method": "provider_tty_bootstrap", "returncode": 0}
    if spec.provider == "claude":
        command = [str(args.engine), "claude-channel", "send", "--session-id", state["session_id"], "--text", text]
    elif spec.provider == "cursor":
        command = [str(args.engine), "cursor-helm", "send", "--session-id", state["session_id"], "--text", text]
    else:
        if process.process.poll() is not None:
            raise RuntimeError(f"{spec.provider} terminal control owner is no longer live")
        process.send(text + "\r")
        return {"method": "provider_tty_bootstrap" if initial else "provider_tty", "returncode": 0}
    deadline = time.monotonic() + 30 if spec.provider == "cursor" else time.monotonic()
    attempts = 0
    completed: subprocess.CompletedProcess[str]
    while True:
        attempts += 1
        completed = subprocess.run(command, cwd=args.repo_root, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode == 0:
            break
        detail = f"{completed.stdout}\n{completed.stderr}"
        cursor_not_idle = "provider_not_idle" in detail or "provider is not idle" in detail.lower()
        if spec.provider != "cursor" or not cursor_not_idle or time.monotonic() >= deadline:
            raise RuntimeError(f"{spec.provider} managed control send failed: {completed.stderr[-1000:]}")
        # Cursor's native TUI can publish its Helm lease before it reaches its
        # first idle prompt. The managed socket rejects this request without
        # injecting text, so retrying is safe and keeps provider control
        # authoritative instead of falling back to a raw PTY write.
        time.sleep(0.25)
    return {
        "method": "longhouse_control",
        "returncode": completed.returncode,
        "attempts": attempts,
        "stdout": completed.stdout[-2000:],
    }


def _stop(spec: ProviderSpec, args: argparse.Namespace, state: dict[str, Any], process: PtyProcess, *, force: bool) -> dict[str, Any]:
    pid = process.pid
    provider_pid = _provider_process_pid(spec, state)
    control_returncode: int | None = None
    if force:
        process.kill_group(signal.SIGKILL)
        method = "sigkill_exact_owner_group"
    elif spec.provider == "cursor":
        control_result = subprocess.run(
            [str(args.engine), "cursor-helm", "stop", "--session-id", state["session_id"]],
            cwd=args.repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        control_returncode = control_result.returncode
        method = "cursor_helm_stop"
    elif spec.provider == "opencode":
        control_result = subprocess.run(
            [str(args.engine), "opencode-bridge", "stop", "--session-id", state["session_id"]],
            cwd=args.repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        control_returncode = control_result.returncode
        method = "opencode_bridge_stop"
    else:
        process.send("\x04")
        method = "claude_terminal_eof"
    exit_code = process.wait(30 if spec.provider == "opencode" else 10)
    fallback_signal: str | None = None
    if exit_code is None:
        fallback_signal = "SIGKILL" if force else "SIGTERM"
        process.kill_group(signal.SIGKILL if force else signal.SIGTERM)
        exit_code = process.wait(5)
    group_dead = _wait_process_group_dead(pid)
    provider_force_signal_sent = force and _signal_pid_if_alive(provider_pid, signal.SIGKILL)
    provider_process_dead = _wait_pid_dead(provider_pid)
    dead = process.process.poll() is not None and group_dead and provider_process_dead
    # OpenCode's TUI wrapper can exit with status 1 when its deliberately
    # controlled localhost server is stopped underneath it. The authoritative
    # clean-shutdown fact is the native bridge control result plus exact owner
    # cleanup; treating the wrapper's exit code as the oracle would turn a
    # successful provider stop into a false qualification failure.
    clean = (
        dead
        and not force
        and (
            (spec.provider == "opencode" and control_returncode == 0)
            or (spec.provider != "opencode" and fallback_signal is None and exit_code == 0)
        )
    )
    return {
        "method": method,
        "pid": pid,
        "provider_pid": provider_pid,
        "exit_code": exit_code,
        "fallback_signal": fallback_signal,
        "process_group_dead": group_dead,
        "provider_force_signal_sent": provider_force_signal_sent,
        "provider_process_dead": provider_process_dead,
        "control_returncode": control_returncode,
        "dead": dead,
        "clean": clean,
    }


def _launch_command(
    spec: ProviderSpec,
    args: argparse.Namespace,
    session_id: str | None,
    *,
    use_credential_files: bool = False,
) -> list[str]:
    command = [
        str(args.longhouse_cli),
        spec.provider,
        "--cwd",
        str(args.repo_root),
        "--url",
        args.api_url,
    ]
    if not use_credential_files:
        command.extend(("--token", args.agents_token))
    command.extend((spec.binary_flag, str(args.provider_bin)))
    if spec.provider == "cursor":
        command.extend(("--permission-mode", "auto_approve"))
    if session_id is not None:
        command.extend((spec.resume_flag, session_id))
    if spec.provider == "cursor":
        cursor_model = os.environ.get("CURSOR_MODEL", "").strip()
        if cursor_model:
            command.extend(("--", "--model", cursor_model))
    return command


def _reconcile_opencode_process_loss(
    args: argparse.Namespace,
    state: dict[str, Any],
    environment: dict[str, str],
) -> dict[str, Any]:
    """Publish the native OpenCode terminal event after an injected loss."""

    command = [str(args.engine), "opencode-bridge", "stop", "--session-id", state["session_id"]]
    claude_dir = str(environment.get("CLAUDE_CONFIG_DIR") or "").strip()
    if claude_dir:
        command.extend(("--claude-dir", claude_dir))
    completed = subprocess.run(
        command,
        cwd=args.repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    receipt = {
        "method": "opencode_bridge_stop_after_process_loss",
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(f"OpenCode process-loss terminal reconciliation failed: {completed.stderr[-1000:]}")
    return receipt


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
    home: Path | None = None
    environment = os.environ.copy()
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    if spec.provider == "opencode":
        configured_model = str(environment.get("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL") or "").strip()
        if configured_model:
            environment["LONGHOUSE_OPENCODE_MODEL"] = (
                configured_model if configured_model.startswith("openrouter/") else f"openrouter/{configured_model}"
            )
    initial: PtyProcess | None = None
    resumed: PtyProcess | None = None
    concurrent: PtyProcess | None = None
    shipper: TranscriptShipper | None = None
    states: list[dict[str, Any]] = []
    final_cleanup: dict[str, Any] = {"verified": False}
    try:
        home = _isolated_provider_home()
        # Make every provider path explicit before any provider or engine
        # process starts. The factory must never inherit the operator's home.
        environment["HOME"] = str(home)
        environment["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        environment["CURSOR_HOME"] = str(home / ".cursor")
        if spec.provider == "claude":
            onboarding = _prepare_claude_profile(
                binary=args.provider_bin,
                home=home,
                workspace=args.repo_root,
                environment=environment,
                recording=root / "claude-onboarding.tty",
            )
            _write_json(root / "claude-onboarding-receipt.json", onboarding)
        shipper = _start_transcript_shipper(
            provider,
            args,
            home=home,
            environment=environment,
            evidence_root=root,
        )
        _write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        if spec.provider == "cursor":
            cursor_hooks = subprocess.run(
                [str(args.engine), "cursor-helm", "configure-hooks", "--cursor-dir", environment["CURSOR_HOME"]],
                cwd=args.repo_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            _write_json(
                root / "cursor-hook-configure-receipt.json",
                {
                    "returncode": cursor_hooks.returncode,
                    "stdout": cursor_hooks.stdout[-2000:],
                    "stderr": cursor_hooks.stderr[-2000:],
                    "cursor_dir": environment["CURSOR_HOME"],
                },
            )
            if cursor_hooks.returncode != 0:
                raise RuntimeError(f"Cursor native hook configuration failed: {cursor_hooks.stderr[-1000:]}")
        initial = PtyProcess(
            _launch_command(spec, args, None, use_credential_files=True),
            cwd=args.repo_root,
            env=environment,
            recording=root / "initial.tty",
        )
        initial_state = _wait_state(spec, home, process=initial)
        if spec.provider == "cursor":
            _wait_cursor_tui_ready(initial, root / "initial.tty")
        elif spec.provider == "claude":
            _wait_claude_tui_ready(initial, root / "initial.tty")
        else:
            initial.settle()
        if spec.provider == "opencode":
            _wait_opencode_tui_ready(initial, root / "initial.tty")
        states.append(initial_state)
        _write_json(root / "initial-bridge-state.json", _redact_state_for_evidence(initial_state))
        initial_provider_pid = _provider_process_pid(spec, initial_state)
        seed_marker = f"LONGHOUSE_{provider.upper()}_RESUME_SEED_{uuid.uuid4().hex}"
        initial_send = _control_send(
            spec,
            args,
            initial_state,
            initial,
            f"Reply exactly {seed_marker} and nothing else.",
            initial=True,
        )
        _write_json(root / "initial-seed-send.json", initial_send)
        _write_json(root / "initial-transcript-ship-receipt.json", shipper.flush("initial"))
        initial_tail = _wait_assistant_marker(
            args.api_url, args.agents_token, initial_state["session_id"], seed_marker, timeout=args.live_send_timeout_secs
        )
        if spec.provider == "cursor":
            _wait_cursor_idle(initial_state, environment)
        _write_json(root / "initial-transcript.jsonl", initial_tail)

        transition = _stop(spec, args, initial_state, initial, force=args.variant == "process_loss")
        if args.variant == "process_loss" and spec.provider == "opencode":
            transition["terminal_reconciliation"] = _reconcile_opencode_process_loss(args, initial_state, environment)
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
            use_credential_files=True,
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
        if spec.provider == "cursor":
            _wait_cursor_tui_ready(resumed, root / "native-resume.tty")
        elif spec.provider == "claude":
            _wait_claude_tui_ready(resumed, root / "native-resume.tty")
        else:
            resumed.settle()
        if spec.provider == "opencode":
            _wait_opencode_tui_ready(resumed, root / "native-resume.tty")
        if spec.provider == "cursor":
            _wait_cursor_idle(resumed_state, environment)
        states.append(resumed_state)
        _write_json(root / "resumed-bridge-state.json", _redact_state_for_evidence(resumed_state))
        resumed_provider_pid = _provider_process_pid(spec, resumed_state)
        post_marker = f"LONGHOUSE_{provider.upper()}_RESUME_POST_{uuid.uuid4().hex}"
        prior_tail = _api_json(
            args.api_url,
            args.agents_token,
            f"sessions/{resumed_state['session_id']}/tail?limit=100&roles=user,assistant",
        )
        prior_assistant_event_digests = _assistant_event_digests(prior_tail)
        post_send = _control_send(spec, args, resumed_state, resumed, f"Reply exactly {post_marker} and nothing else.")
        _write_json(root / "post-resume-send.json", post_send)
        _write_json(root / "post-resume-transcript-ship-receipt.json", shipper.flush("post-resume"))
        if spec.provider == "claude":
            resumed_tail, response_correlation = _wait_assistant_response_after_marker(
                args.api_url,
                args.agents_token,
                resumed_state["session_id"],
                post_marker,
                prior_assistant_event_digests=prior_assistant_event_digests,
                timeout=args.live_send_timeout_secs,
            )
            post_resume_response_correlated = bool(
                response_correlation["marker_observed_in_transcript"] and response_correlation["new_assistant_events"] > 0
            )
            post_resume_provider_activity = response_correlation["new_assistant_events"] > 0
        else:
            resumed_tail = _wait_assistant_marker(
                args.api_url, args.agents_token, resumed_state["session_id"], post_marker, timeout=args.live_send_timeout_secs
            )
            assistant_marker_observed = _assistant_contains(resumed_tail, post_marker)
            observed_assistant_event_digests = _assistant_event_digests(resumed_tail)
            new_assistant_events = observed_assistant_event_digests - prior_assistant_event_digests
            response_correlation = {
                "method": "assistant_marker_echo",
                "marker_observed_in_assistant": assistant_marker_observed,
                "prior_assistant_events": len(prior_assistant_event_digests),
                "observed_assistant_events": len(observed_assistant_event_digests),
                "new_assistant_events": len(new_assistant_events),
            }
            post_resume_response_correlated = bool(
                response_correlation["marker_observed_in_assistant"] and response_correlation["new_assistant_events"] > 0
            )
            post_resume_provider_activity = response_correlation["new_assistant_events"] > 0
        _write_json(root / "post-resume-response-correlation.json", response_correlation)
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
        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        redacted = _secret_scan(root, list(_qualification_secrets(environment, args.agents_token)))
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
            "post_resume_provider_activity": post_resume_provider_activity,
            "post_resume_response_correlated": post_resume_response_correlated,
            "post_resume_marker_in_assistant_transcript": spec.provider != "claude" and post_resume_marker_observed,
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
        try:
            if home is not None:
                _write_json(root / "state-candidates.json", _state_candidate_diagnostics(spec, home))
        except (OSError, TypeError):
            pass
        final_cleanup = _cleanup_processes(spec, (initial, resumed, concurrent), states)
        _write_json(root / "cleanup-receipt.json", final_cleanup)
        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        redacted = _secret_scan(root, list(_qualification_secrets(environment, args.agents_token)))
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
        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
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
