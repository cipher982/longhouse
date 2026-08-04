#!/usr/bin/env python3
"""Qualify the installed managed-provider launch path during a host outage.

This lane intentionally does not send a provider prompt. It resolves the
actual provider executables from PATH (or explicit overrides), starts each
provider through the native ``longhouse`` facade while the Runtime Host is
down, verifies the provider reaches local ownership with a bounded degraded
message, then restarts the same Runtime Host and verifies the Machine Agent
drains the durable registration intents.

The protocol-double lifecycle smoke remains the broader control and cleanup
test. This script closes the separate installed-binary qualification boundary.
It uses disposable Longhouse/provider state and never writes to the user's
provider configuration directories.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import pty
import re
import secrets
import select
import shutil
import sqlite3
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PROVIDERS = ("claude", "codex", "opencode", "cursor")
DEVICE_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_BINARIES = {
    "claude": ("claude", "LONGHOUSE_CLAUDE_BIN", "--claude-bin"),
    "codex": ("codex", "LONGHOUSE_CODEX_BIN", "--codex-bin"),
    "opencode": ("opencode", "LONGHOUSE_OPENCODE_BIN", "--opencode-bin"),
    "cursor": ("cursor-agent", "LONGHOUSE_CURSOR_BIN", "--cursor-bin"),
}
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


@dataclass
class CommandEvidence:
    returncode: int | None
    output: str
    marker_seen: bool
    timed_out: bool


class ProviderLaunchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None,
        output: str,
        timed_out: bool,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.output = output
        self.timed_out = timed_out


@dataclass
class LiveCommand:
    process: subprocess.Popen[bytes]
    output_fd: int
    output: bytearray
    is_tty: bool
    provider_ready_observed: bool


@dataclass
class Host:
    process: subprocess.Popen[bytes]
    port: int
    log_path: Path


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_file(raw: str | None, name: str) -> Path:
    candidate = Path(raw).expanduser() if raw else None
    if candidate is None:
        resolved = shutil.which(name)
        if resolved is None:
            raise RuntimeError(f"{name} was not found on PATH")
        candidate = Path(resolved)
    candidate = candidate.resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError(f"provider executable is not runnable: {candidate}")
    return candidate


def version_probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    output = (result.stdout or result.stderr or "").strip()
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "returncode": result.returncode,
        "version_line": output.splitlines()[0] if output else "",
    }


def choose_port() -> int:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_is_available(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            # This also waits out the short TCP lifecycle after a Runtime Host
            # restart; a connect-only check cannot distinguish TIME_WAIT from
            # a live listener, while uvicorn still rejects the former here.
            return False
    return True


def kill_group(process: subprocess.Popen[Any] | int, *, grace: float = 0.5) -> None:
    pid = process if isinstance(process, int) else process.pid
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid == os.getpgrp():
        return
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            return
        except PermissionError:
            # `uv` may hand the server to a child process whose group is no
            # longer owned by the short-lived launcher. The Popen PID is still
            # an exact target, so fall back to that child rather than probing
            # or killing an unrelated process group.
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                return
        if signum == signal.SIGTERM:
            time.sleep(grace)


def wait_for(predicate: Any, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {description}")


def http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    body = None
    headers: dict[str, str] = {}
    if token:
        headers["X-Agents-Token"] = token
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            value = json.loads(raw) if raw else {}
            return response.status, value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = {"body": raw}
        return error.code, value if isinstance(value, dict) else {}


def isolated_profile_env(env: dict[str, str], root: Path) -> dict[str, str]:
    profile_root = root / "runtime-profile"
    env.update(
        {
            "HOME": str(profile_root / "home"),
            "XDG_CONFIG_HOME": str(profile_root / "config"),
            "XDG_DATA_HOME": str(profile_root / "data"),
            "XDG_STATE_HOME": str(profile_root / "state"),
            "XDG_CACHE_HOME": str(profile_root / "cache"),
            "CODEX_HOME": str(profile_root / "codex"),
        }
    )
    return env


def runtime_env(root: Path, engine_bin: Path) -> dict[str, str]:
    env = isolated_profile_env(os.environ.copy(), root)
    env.update(
        {
            "LONGHOUSE_HOME": str(root / "longhouse"),
            "LONGHOUSE_ENGINE_BIN": str(engine_bin),
            "PYTHONUNBUFFERED": "1",
            "TERM": env.get("TERM", "xterm-256color"),
        }
    )
    return env


def runtime_host_archive_database(root: Path) -> Path:
    return root / "runtime-host.db"


def runtime_host_live_database(root: Path) -> Path:
    # The Runtime Host derives the catalogd-owned live store by inserting
    # ``-live`` before the archive database suffix. Keep the assertion tied to
    # that one path derivation instead of silently accepting a missing DB.
    archive = runtime_host_archive_database(root)
    return archive.with_name(f"{archive.stem}-live{archive.suffix}")


def start_host(root: Path, evidence_root: Path, *, port: int, ordinal: int) -> Host:
    repo_root = Path(__file__).resolve().parents[2]
    server_root = repo_root / "server"
    venv_python = server_root / ".venv" / "bin" / "python"
    wait_for(
        lambda: port_is_available(port),
        60,
        f"Runtime Host port {port} to become available",
    )
    uv = shutil.which("uv")
    if venv_python.is_file():
        command = [
            str(venv_python),
            "-m",
            "zerg.cli.main",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        command_cwd = server_root
    else:
        if uv is None:
            raise RuntimeError("neither server/.venv/bin/python nor uv was found")
        command = [
            uv,
            "run",
            "--project",
            str(server_root),
            "python",
            "-m",
            "zerg.cli.main",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        command_cwd = repo_root
    database = runtime_host_archive_database(root)
    log_path = evidence_root / f"runtime-host-{ordinal}.log"
    log = log_path.open("wb")
    env = isolated_profile_env(os.environ.copy(), root)
    env.update(
        {
            "AUTH_DISABLED": "1",
            "LLM_DISABLED": "1",
            "LOG_LEVEL": "WARNING",
            "DATABASE_URL": f"sqlite:///{database}",
            "JWT_SECRET": secrets.token_urlsafe(32),
            "FERNET_SECRET": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
            "INTERNAL_API_SECRET": secrets.token_urlsafe(32),
            "PYTHONUNBUFFERED": "1",
        }
    )
    env["PYTHONPATH"] = str(server_root) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        command,
        cwd=str(command_cwd),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    def healthy() -> bool:
        try:
            code, _ = http_json(f"{base_url}/api/health", timeout=0.5)
            return code == 200
        except (OSError, urllib.error.URLError):
            return False

    try:
        wait_for(healthy, 45, f"Runtime Host on {base_url}")
    except Exception:
        kill_group(process)
        process.wait(timeout=5)
        log.close()
        raise
    log.close()
    return Host(process=process, port=port, log_path=log_path)


def stop_host(host: Host | None) -> None:
    if host is None:
        return
    kill_group(host.process)
    try:
        host.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill_group(host.process, grace=0.1)
        host.process.wait(timeout=5)


def stop_processes_for_root(root: Path) -> None:
    """Reap detached Runtime Host workers for this disposable test root."""

    needle = str(root)
    for _ in range(2):
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
        pids: list[int] = []
        for line in listing.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or needle not in fields[1]:
                continue
            try:
                pid = int(fields[0])
            except ValueError:
                continue
            if pid != os.getpid():
                pids.append(pid)
        for pid in pids:
            kill_group(pid, grace=0.1)
        if pids:
            time.sleep(0.2)


def create_device_token(base_url: str) -> str:
    code, body = http_json(
        f"{base_url}/api/devices/tokens",
        method="POST",
        payload={
            "name": "installed-managed-launch-fault-matrix",
            "device_id": DEVICE_ID,
        },
    )
    token = body.get("token")
    if (
        code not in {200, 201}
        or not isinstance(token, str)
        or not token.startswith("zdt_")
    ):
        raise RuntimeError(
            f"Runtime Host did not issue a device token: status={code} body={body}"
        )
    return token


def read_retry_count(root: Path) -> int:
    directory = root / "longhouse" / "agent" / "managed-local" / "registration-retries"
    return sum(1 for path in directory.glob("*.json") if path.is_file())


def redact(text: str, token: str) -> str:
    return text.replace(token, "<device-token-redacted>")


def session_process_groups(session_id: str) -> set[int]:
    listing = subprocess.run(
        ["ps", "-Ao", "pid=,pgid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    groups: set[int] = set()
    for line in listing.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or session_id not in fields[2]:
            continue
        try:
            pid = int(fields[0])
            pgid = int(fields[1])
        except ValueError:
            continue
        if pid != os.getpid() and pgid != os.getpgrp():
            groups.add(pgid)
    return groups


def cleanup_detached_provider(
    provider: str,
    session_id: str | None,
    *,
    longhouse_bin: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    if provider not in {"codex", "opencode"} or not session_id:
        return {"status": "not_applicable", "remaining_process_groups": []}
    stop = subprocess.run(
        [str(longhouse_bin), provider, "stop", "--session-id", session_id],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    groups = session_process_groups(session_id)
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, PermissionError):
            pass
    time.sleep(0.5)
    remaining = sorted(session_process_groups(session_id))
    for pgid in remaining:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, PermissionError):
            pass
    time.sleep(0.2)
    return {
        "status": "pass" if not session_process_groups(session_id) else "fail",
        "stop_returncode": stop.returncode,
        "stop_output": redact(
            (stop.stdout or "") + (stop.stderr or ""),
            env.get("LONGHOUSE_DEVICE_TOKEN", ""),
        ),
        "remaining_process_groups": sorted(session_process_groups(session_id)),
    }


def wait_status(pid: int, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if waited_pid == pid:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def run_tty_command(
    command: list[str],
    env: dict[str, str],
    *,
    marker: str,
    timeout: float = 45,
) -> CommandEvidence:
    pid, master = pty.fork()
    if pid == 0:
        os.execvpe(command[0], command, env)
    output = bytearray()
    marker_seen = False
    sent_interrupt = False
    marker_at: float | None = None
    started = time.monotonic()
    try:
        while time.monotonic() - started < timeout:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    output.extend(os.read(master, 8192))
                except OSError:
                    break
            decoded = output.decode("utf-8", errors="replace")
            if marker in decoded and not sent_interrupt:
                marker_seen = True
                marker_at = marker_at or time.monotonic()
            if (
                marker_seen
                and not sent_interrupt
                and marker_at is not None
                and time.monotonic() - marker_at >= 3
            ):
                sent_interrupt = True
                try:
                    os.write(master, b"\x03")
                except OSError:
                    pass
                try:
                    os.killpg(os.getpgid(pid), signal.SIGINT)
                except (OSError, PermissionError):
                    pass
            if sent_interrupt and wait_status(pid, 0.0) is not None:
                break
        timed_out = wait_status(pid, 0.0) is None
        if timed_out:
            kill_group(pid, grace=0.2)
        returncode = wait_status(pid, 5)
        return CommandEvidence(
            returncode=returncode,
            output=output.decode("utf-8", errors="replace"),
            marker_seen=marker_seen,
            timed_out=timed_out,
        )
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def run_pipe_command(
    command: list[str],
    env: dict[str, str],
    *,
    marker: str,
    timeout: float = 30,
) -> CommandEvidence:
    process = subprocess.Popen(
        command,
        env=env,
        cwd=env.get("LONGHOUSE_FAULT_CWD"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            output = error.stdout or ""
            kill_group(process, grace=0.2)
            tail, _ = process.communicate(timeout=5)
            output += tail or ""
        return CommandEvidence(
            returncode=process.returncode,
            output=output,
            marker_seen=marker in output,
            timed_out=timed_out,
        )
    finally:
        # Detached provider bridges deliberately outlive the facade. The
        # qualification lane must still leave no provider process behind.
        if process.poll() is None:
            kill_group(process, grace=0.2)


def _read_live_output(command: LiveCommand) -> None:
    try:
        command.output.extend(os.read(command.output_fd, 8192))
    except OSError:
        pass


def start_live_command(
    command: list[str],
    env: dict[str, str],
    *,
    use_tty: bool,
    marker: str,
    ready_check: Callable[[int], bool] | None = None,
    timeout: float = 45,
) -> LiveCommand:
    """Start a provider with a real process handle and wait only for startup.

    ``pty.openpty`` plus ``Popen`` is safe to call from the concurrent launch
    pool. The returned process remains alive until Runtime Host recovery has
    been asserted, so the registration retry cannot silently settle as a
    post-hoc provider abort.
    """

    slave_fd: int | None = None
    if use_tty:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            command,
            env=env,
            cwd=env.get("LONGHOUSE_FAULT_CWD"),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        output_fd = master_fd
    else:
        process = subprocess.Popen(
            command,
            env=env,
            cwd=env.get("LONGHOUSE_FAULT_CWD"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if process.stdout is None:
            raise RuntimeError("provider stdout pipe was not created")
        output_fd = process.stdout.fileno()
        os.set_blocking(output_fd, False)

    live = LiveCommand(
        process=process,
        output_fd=output_fd,
        output=bytearray(),
        is_tty=use_tty,
        provider_ready_observed=False,
    )
    started = time.monotonic()
    try:
        while time.monotonic() - started < timeout:
            readable, _, _ = select.select([output_fd], [], [], 0.2)
            if readable:
                _read_live_output(live)
            marker_seen = marker in live.output.decode("utf-8", errors="replace")
            if marker_seen and (ready_check is None or ready_check(process.pid)):
                live.provider_ready_observed = True
                return live
            if process.poll() is not None:
                _read_live_output(live)
                raise ProviderLaunchError(
                    f"provider exited before startup marker: {process.returncode}",
                    returncode=process.returncode,
                    output=live.output.decode("utf-8", errors="replace"),
                    timed_out=False,
                )
        kill_group(process, grace=0.2)
        process.wait(timeout=5)
        raise ProviderLaunchError(
            f"provider did not emit startup marker within {timeout}s",
            returncode=process.returncode,
            output=live.output.decode("utf-8", errors="replace"),
            timed_out=True,
        )
    except BaseException:
        if use_tty:
            try:
                os.close(output_fd)
            except OSError:
                pass
        elif process.stdout is not None:
            process.stdout.close()
        raise


def finish_live_command(command: LiveCommand) -> dict[str, Any]:
    """Stop one exact provider process group and assert it is reaped."""

    if command.is_tty:
        try:
            os.write(command.output_fd, b"\x03")
        except OSError:
            pass
        try:
            os.killpg(os.getpgid(command.process.pid), signal.SIGINT)
        except (OSError, PermissionError):
            pass
    if command.process.poll() is None:
        kill_group(command.process, grace=0.2)
    try:
        command.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill_group(command.process, grace=0.1)
        command.process.wait(timeout=5)
    if command.is_tty:
        try:
            os.close(command.output_fd)
        except OSError:
            pass
    elif command.process.stdout is not None:
        command.process.stdout.close()
    return {
        "returncode": command.process.returncode,
        "process_group_reaped": command.process.poll() is not None,
    }


def provider_command(
    provider: str,
    *,
    longhouse_bin: Path,
    provider_bin: Path,
    root: Path,
    cwd: Path,
    base_url: str,
    token: str,
    keep_attached: bool = False,
) -> list[str]:
    _, _, provider_flag = PROVIDER_BINARIES[provider]
    command = [
        str(longhouse_bin),
        provider,
        "--url",
        base_url,
        "--token",
        token,
        "--cwd",
        str(cwd),
        provider_flag,
        str(provider_bin),
    ]
    if provider == "claude":
        command.extend(["--claude-dir", str(root / "provider-config" / "claude")])
    elif provider == "opencode":
        if not keep_attached:
            command.append("--no-attach")
        command.extend(["--claude-dir", str(root / "provider-config" / "opencode")])
    elif provider == "codex":
        if not keep_attached:
            command.append("--no-attach")
    else:
        command.extend(
            [
                "--config-dir",
                str(root / "provider-config" / "cursor"),
                "--permission-mode",
                "auto_approve",
                "--verbose",
            ]
        )
    return command


def retry_owner_ready(root: Path, provider: str, launcher_pid: int) -> bool:
    directory = root / "longhouse" / "agent" / "managed-local" / "registration-retries"
    for path in directory.glob("*.json"):
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            str(intent.get("provider_name", "")).lower() == provider.lower()
            and intent.get("launcher_pid") == launcher_pid
            and intent.get("provider_ready") is True
        ):
            return True
    return False


def provider_failure_qualification(provider: str, output: str) -> str:
    # Claude's native channel probe depends on provider authentication. An
    # isolated qualification profile intentionally has no credentials, so do
    # not mislabel that harness precondition failure as a provider-owned
    # startup defect.
    if provider == "claude" and (
        "auth status" in output.lower()
        or "native channels unavailable" in output.lower()
    ):
        return "harness_precondition_unmet"
    return "provider_owned_start_failure"


def run_provider_live(
    provider: str,
    *,
    longhouse_bin: Path,
    provider_bin: Path,
    root: Path,
    evidence_root: Path,
    base_url: str,
    token: str,
    engine_bin: Path,
) -> tuple[dict[str, Any], LiveCommand | None]:
    provider_root = evidence_root / "providers" / provider
    provider_root.mkdir(parents=True, exist_ok=True)
    cwd = root / "cwd" / provider
    cwd.mkdir(parents=True, exist_ok=True)
    env = runtime_env(root, engine_bin)
    env["LONGHOUSE_FAULT_CWD"] = str(cwd)
    env["CODEX_HOME"] = str(root / "provider-config" / "codex")
    provider_home = root / "provider-home" / provider
    env["HOME"] = str(provider_home)
    env["XDG_CONFIG_HOME"] = str(provider_home / "config")
    env["XDG_DATA_HOME"] = str(provider_home / "data")
    env["XDG_STATE_HOME"] = str(provider_home / "state")
    env["XDG_CACHE_HOME"] = str(provider_home / "cache")
    command = provider_command(
        provider,
        longhouse_bin=longhouse_bin,
        provider_bin=provider_bin,
        root=root,
        cwd=cwd,
        base_url=base_url,
        token=token,
    )
    try:
        live = start_live_command(
            command,
            env,
            use_tty=True,
            marker="degraded Helm mode",
            ready_check=lambda launcher_pid: retry_owner_ready(
                root, provider, launcher_pid
            ),
        )
    except ProviderLaunchError as error:
        output = redact(error.output, token)
        launch_log = provider_root / "launch.log"
        launch_log.write_text(output, encoding="utf-8")
        session_match = UUID_RE.search(output)
        launch_intent_created = False
        if session_match:
            try:
                launch_intent_created = any(
                    str(intent.get("provider_name", "")).lower() == provider.lower()
                    and str(intent.get("expected_session_id")) == session_match.group(0)
                    for intent in read_retry_intents(root)
                )
            except (OSError, json.JSONDecodeError):
                launch_intent_created = False
        return (
            {
                "provider": provider,
                "provider_binary": str(provider_bin),
                "provider_version": version_probe(provider_bin),
                "facade": str(longhouse_bin),
                "command": [
                    "<device-token-redacted>" if value == token else value
                    for value in command
                ],
                "returncode": error.returncode,
                "timed_out": error.timed_out,
                "degraded_marker_seen": "degraded Helm mode" in output,
                "provider_ready_observed": False,
                "launcher_pid": None,
                "session_id_observed": session_match.group(0) if session_match else None,
                "launch_intent_created": launch_intent_created,
                "startup_failure": str(error),
                "qualification": provider_failure_qualification(provider, output),
                "launch_log": str(launch_log),
                "retry_intents_after_launch": read_retry_count(root),
            },
            None,
        )
    output = live.output.decode("utf-8", errors="replace")
    output = redact(output, token)
    (provider_root / "launch.log").write_text(output, encoding="utf-8")
    session_match = UUID_RE.search(output)
    launch_intent_created = any(
        str(intent.get("provider_name", "")).lower() == provider.lower()
        for intent in read_retry_intents(root)
    )
    result = {
        "provider": provider,
        "provider_binary": str(provider_bin),
        "provider_version": version_probe(provider_bin),
        "facade": str(longhouse_bin),
        "command": [
            "<device-token-redacted>" if value == token else value for value in command
        ],
        "returncode": None,
        "timed_out": False,
        "degraded_marker_seen": True,
        "provider_ready_observed": live.provider_ready_observed,
        "launcher_pid": live.process.pid,
        "session_id_observed": session_match.group(0) if session_match else None,
        "launch_intent_created": launch_intent_created,
        "launch_log": str(provider_root / "launch.log"),
        "retry_intents_after_launch": read_retry_count(root),
    }
    return result, live


def run_provider(
    provider: str,
    *,
    longhouse_bin: Path,
    provider_bin: Path,
    root: Path,
    evidence_root: Path,
    base_url: str,
    token: str,
    engine_bin: Path,
) -> dict[str, Any]:
    provider_root = evidence_root / "providers" / provider
    provider_root.mkdir(parents=True, exist_ok=True)
    cwd = root / "cwd" / provider
    cwd.mkdir(parents=True, exist_ok=True)
    env = runtime_env(root, engine_bin)
    env["LONGHOUSE_FAULT_CWD"] = str(cwd)
    env["CODEX_HOME"] = str(root / "provider-config" / "codex")
    provider_home = root / "provider-home" / provider
    env["HOME"] = str(provider_home)
    env["XDG_CONFIG_HOME"] = str(provider_home / "config")
    env["XDG_DATA_HOME"] = str(provider_home / "data")
    env["XDG_STATE_HOME"] = str(provider_home / "state")
    env["XDG_CACHE_HOME"] = str(provider_home / "cache")
    command = provider_command(
        provider,
        longhouse_bin=longhouse_bin,
        provider_bin=provider_bin,
        root=root,
        cwd=cwd,
        base_url=base_url,
        token=token,
    )
    if provider in {"claude", "cursor"}:
        evidence = run_tty_command(command, env, marker="degraded Helm mode")
    else:
        evidence = run_pipe_command(command, env, marker="degraded Helm mode")
    output = redact(evidence.output, token)
    (provider_root / "launch.log").write_text(output, encoding="utf-8")
    session_match = UUID_RE.search(output)
    cleanup = cleanup_detached_provider(
        provider,
        session_match.group(0) if session_match else None,
        longhouse_bin=longhouse_bin,
        env=env,
    )
    result = {
        "provider": provider,
        "provider_binary": str(provider_bin),
        "provider_version": version_probe(provider_bin),
        "facade": str(longhouse_bin),
        "command": [
            "<device-token-redacted>" if value == token else value for value in command
        ],
        "returncode": evidence.returncode,
        "timed_out": evidence.timed_out,
        "degraded_marker_seen": evidence.marker_seen,
        "session_id_observed": session_match.group(0) if session_match else None,
        "launch_log": str(provider_root / "launch.log"),
        "retry_intents_after_launch": read_retry_count(root),
        "detached_cleanup": cleanup,
    }
    if not evidence.marker_seen:
        raise RuntimeError(
            f"{provider} installed launch did not report degraded Helm mode; see {provider_root / 'launch.log'}"
        )
    if evidence.timed_out:
        raise RuntimeError(f"{provider} installed degraded launch exceeded its bound")
    return result


def run_concurrent_providers(
    providers: tuple[str, ...],
    *,
    longhouse_bin: Path,
    provider_bins: dict[str, Path],
    root: Path,
    evidence_root: Path,
    base_url: str,
    token: str,
    engine_bin: Path,
) -> list[tuple[dict[str, Any], LiveCommand | None]]:
    """Launch the selected installed providers at the same time.

    The provider launchers own their process groups and their provider-specific
    terminal adapters. Running the existing bounded single-provider probe in a
    pool preserves those ownership checks while exposing shared-agent races.
    """

    with ThreadPoolExecutor(
        max_workers=len(providers), thread_name_prefix="installed-fault"
    ) as pool:
        futures = {
            provider: pool.submit(
                run_provider_live,
                provider,
                longhouse_bin=longhouse_bin,
                provider_bin=provider_bins[provider],
                root=root,
                evidence_root=evidence_root / "concurrent",
                base_url=base_url,
                token=token,
                engine_bin=engine_bin,
            )
            for provider in providers
        }
        return [futures[provider].result() for provider in providers]


def read_retry_intents(root: Path) -> list[dict[str, Any]]:
    directory = root / "longhouse" / "agent" / "managed-local" / "registration-retries"
    intents: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        intents.append(json.loads(path.read_text(encoding="utf-8")))
    return intents


def launch_attempt_states(
    database: Path, session_ids: list[str]
) -> dict[str, str | None]:
    if not database.is_file():
        raise RuntimeError(
            f"Runtime Host launch-state database is missing: {database}"
        )
    try:
        with sqlite3.connect(database, timeout=0.5) as connection:
            rows = connection.execute(
                "SELECT session_id, state FROM live_session_launch_attempts "
                "WHERE session_id IN ({}) ORDER BY id".format(
                    ",".join("?" for _ in session_ids)
                ),
                session_ids,
            ).fetchall()
    except sqlite3.Error as error:
        raise RuntimeError(
            f"could not inspect Runtime Host launch states in {database}: {error}"
        ) from error
    states: dict[str, str | None] = {session_id: None for session_id in session_ids}
    for session_id, state in rows:
        states[str(session_id)] = str(state)
    return states


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = (
        args.evidence_root
        or repo_root / ".build/canaries/installed-managed-launch-fault-matrix" / stamp
    ).resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    args._resolved_evidence_root = evidence_root
    # Keep this root short: Codex's bridge IPC socket is subject to macOS
    # SUN_LEN, and the qualification must exercise the installed path rather
    # than fail because the test harness chose a long temporary directory.
    temp_root = Path(tempfile.mkdtemp(prefix="lh.", dir="/tmp"))
    args._temporary_root = temp_root
    host: Host | None = None
    engine: subprocess.Popen[bytes] | None = None
    engine_handle: Any | None = None
    engine_log: Path | None = None
    results: list[dict[str, Any]] = []
    live_commands: list[tuple[dict[str, Any], LiveCommand]] = []
    args._matrix_results = results
    selected_providers = tuple(args.provider or PROVIDERS)
    args._selected_providers = selected_providers
    try:
        longhouse_bin = resolve_file(
            args.longhouse_bin or os.environ.get("LONGHOUSE_FAULT_LONGHOUSE_BIN"),
            "longhouse",
        )
        engine_bin = resolve_file(
            args.engine_bin or os.environ.get("LONGHOUSE_FAULT_ENGINE_BIN"),
            "longhouse-engine",
        )
        provider_bins: dict[str, Path] = {}
        provider_evidence: dict[str, Any] = {}
        for provider in selected_providers:
            binary_name, env_name, _ = PROVIDER_BINARIES[provider]
            provider_bins[provider] = resolve_file(
                os.environ.get(env_name), binary_name
            )
            provider_evidence[provider] = version_probe(provider_bins[provider])

        port = choose_port()
        host = start_host(temp_root, evidence_root, port=port, ordinal=1)
        base_url = f"http://127.0.0.1:{port}"
        token = create_device_token(base_url)
        stop_host(host)
        stop_processes_for_root(temp_root)
        host = None

        for provider in selected_providers:
            result, live = run_provider_live(
                provider,
                longhouse_bin=longhouse_bin,
                provider_bin=provider_bins[provider],
                root=temp_root,
                evidence_root=evidence_root,
                base_url=base_url,
                token=token,
                engine_bin=engine_bin,
            )
            results.append(result)
            if live is not None:
                live_commands.append((result, live))
        concurrent_results: list[dict[str, Any]] = []
        if args.concurrent:
            concurrent_results = run_concurrent_providers(
                selected_providers,
                longhouse_bin=longhouse_bin,
                provider_bins=provider_bins,
                root=temp_root,
                evidence_root=evidence_root,
                base_url=base_url,
                token=token,
                engine_bin=engine_bin,
            )
            for result, live in concurrent_results:
                results.append(result)
                if live is not None:
                    live_commands.append((result, live))
        provider_failures = [
            result
            for result in results
            if result.get("startup_failure") is not None
        ]
        if provider_failures and not args.allow_unqualified_recovery:
            failed_providers = ", ".join(
                str(result.get("provider")) for result in provider_failures
            )
            raise RuntimeError(
                "installed provider startup did not reach the degraded marker for "
                f"{failed_providers}; use --allow-unqualified-recovery to retain "
                "a yellow provider-owned failure artifact"
            )
        retry_count = read_retry_count(temp_root)
        if provider_failures and args.allow_unqualified_recovery:
            if any(
                result.get("provider_version", {}).get("returncode") != 0
                for result in provider_failures
            ):
                raise RuntimeError(
                    "provider-owned startup failure lacks a verified installed "
                    "provider binary identity; refusing a yellow qualification"
                )
            has_degraded_recovery_evidence = any(
                result.get("startup_failure") is None
                and result.get("degraded_marker_seen")
                and result.get("launch_intent_created")
                for result in results
            )
            if not has_degraded_recovery_evidence or retry_count == 0:
                raise RuntimeError(
                    "installed provider startup produced no degraded provider start "
                    "and no attributable durable retry evidence; refusing a yellow "
                    "success for an unstarted provider"
                )
        expected_retry_count = sum(
            1 for result in results if result.get("launch_intent_created")
        )
        if retry_count != expected_retry_count:
            raise RuntimeError(
                f"installed outage launches left {retry_count} retry intents, expected {expected_retry_count}"
            )
        retry_intents = read_retry_intents(temp_root)
        launch_intent_provider_counts = Counter(
            str(result.get("provider", "")).lower()
            for result in results
            if result.get("launch_intent_created")
        )
        intent_provider_counts = Counter(
            str(intent.get("provider_name", "")).lower()
            for intent in retry_intents
        )
        if intent_provider_counts != launch_intent_provider_counts:
            raise RuntimeError(
                "installed launch intents were not attributable one-for-one by provider: "
                f"intents={dict(intent_provider_counts)} "
                f"launch_intents={dict(launch_intent_provider_counts)}"
            )
        args._retry_intents = [
            {
                key: intent.get(key)
                for key in (
                    "provider_name",
                    "expected_session_id",
                    "provider_ready",
                    "provider_pid",
                    "provider_process_start_time",
                    "provider_exited",
                    "launcher_pid",
                    "last_error",
                )
            }
            for intent in retry_intents
        ]
        expected_session_ids = [
            str(intent.get("expected_session_id"))
            for intent in retry_intents
            if intent.get("expected_session_id")
        ]
        if len(expected_session_ids) != expected_retry_count:
            raise RuntimeError(
                "installed recovery did not retain one expected session identity per launch: "
                f"{len(expected_session_ids)} != {expected_retry_count}"
            )

        if any(not intent.get("launcher_pid") for intent in retry_intents):
            raise RuntimeError("installed retry intents contained no launcher ownership identity")
        intent_readiness_by_launcher = {
            intent.get("launcher_pid"): intent.get("provider_ready") is True
            for intent in retry_intents
        }
        for result in results:
            launcher_pid = result.get("launcher_pid")
            if launcher_pid in intent_readiness_by_launcher:
                result["provider_ready_durable"] = intent_readiness_by_launcher[launcher_pid]
        unready_intents = [
            intent
            for intent in retry_intents
            if intent.get("provider_ready") is not True
        ]
        if unready_intents and not args.allow_unqualified_recovery:
            providers = ", ".join(
                str(intent.get("provider_name") or "unknown") for intent in unready_intents
            )
            raise RuntimeError(
                "installed provider launch did not reach provider readiness for "
                f"{providers}; this run cannot qualify host recovery. "
                "Use --allow-unqualified-recovery only to record a yellow degraded-start result."
            )
        harness_precondition_failures = any(
            result.get("qualification") == "harness_precondition_unmet"
            for result in provider_failures
        )

        # A pre-ready intent is intentionally not eligible for Runtime Host
        # registration. End those exact launchers and assert their process
        # groups are reaped. The Machine Agent removes the stale pre-ready
        # intents during its normal owner-local scan after restart.
        pre_ready_cleanup_evidence: dict[str, Any] | None = None
        if unready_intents:
            remaining_live_commands: list[tuple[dict[str, Any], LiveCommand]] = []
            cleanup_results: list[dict[str, Any]] = []
            for result, live in live_commands:
                durable_ready = result.get(
                    "provider_ready_durable", result["provider_ready_observed"]
                )
                if durable_ready is True:
                    remaining_live_commands.append((result, live))
                    continue
                cleanup = finish_live_command(live)
                if not cleanup["process_group_reaped"]:
                    raise RuntimeError(
                        f"{result['provider']} pre-ready provider process was not reaped"
                    )
                result["returncode"] = cleanup["returncode"]
                result["detached_cleanup"] = {
                    "status": "pass",
                    "remaining_process_groups": [],
                    **cleanup,
                }
                cleanup_results.append(
                    {
                        "provider": result["provider"],
                        "launcher_pid": result["launcher_pid"],
                        **cleanup,
                    }
                )
            live_commands = remaining_live_commands
            pre_ready_cleanup_evidence = {
                "provider_processes_reaped": cleanup_results,
                "retry_intents_before_machine_agent_gc": read_retry_count(temp_root),
                "retry_intents_expected_after_gc": len(retry_intents)
                - len(unready_intents),
            }

        ready_session_ids = [
            str(intent["expected_session_id"])
            for intent in retry_intents
            if intent.get("provider_ready") is True
        ]
        ready_ids = set(ready_session_ids)
        unready_session_ids = [
            str(intent["expected_session_id"])
            for intent in retry_intents
            if intent.get("provider_ready") is not True
        ]

        cold_restart_evidence: dict[str, Any] | None = None
        if args.cold_restart:
            cold_log = evidence_root / "machine-agent-cold-restart.log"
            cold_handle = cold_log.open("wb")
            cold_env = runtime_env(temp_root, engine_bin)
            cold_engine = subprocess.Popen(
                [
                    str(engine_bin),
                    "connect",
                    "--url",
                    base_url,
                    "--token",
                    token,
                    "--db",
                    str(temp_root / "agent.db"),
                    "--machine-name",
                    DEVICE_ID,
                    "--fallback-scan-secs",
                    "1",
                    "--spool-replay-secs",
                    "1",
                ],
                env=cold_env,
                stdout=cold_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(2)
            first_start_returncode = cold_engine.poll()
            if first_start_returncode is not None:
                cold_handle.close()
                raise RuntimeError(
                    "cold-start Machine Agent exited during Runtime Host outage; "
                    "local recovery must stay alive: "
                    f"{first_start_returncode}"
                )
            engine = cold_engine
            engine_handle = cold_handle
            engine_log = cold_log
            current_retry_intents = read_retry_intents(temp_root)
            current_retry_ids = {
                str(intent.get("expected_session_id")) for intent in current_retry_intents
            }
            if not ready_ids.issubset(current_retry_ids):
                raise RuntimeError(
                    "cold-start Machine Agent removed a provider-ready retry intent "
                    "during the outage"
                )
            preserved_retry_count = len(current_retry_intents)

            def cold_retry_progress() -> bool:
                return any(
                    int(intent.get("attempts") or 0) > 0
                    for intent in read_retry_intents(temp_root)
                    if str(intent.get("expected_session_id")) in ready_ids
                )

            wait_for(
                cold_retry_progress,
                15,
                "cold-start Machine Agent retry progress while Runtime Host is unavailable",
            )
            retry_attempts_observed = max(
                int(intent.get("attempts") or 0)
                for intent in read_retry_intents(temp_root)
                if str(intent.get("expected_session_id")) in ready_ids
            )
            cold_restart_evidence = {
                "first_start_returncode": first_start_returncode,
                "first_start_survived_outage": True,
                "retry_attempts_observed": retry_attempts_observed,
                "retry_intents_after_cold_start": preserved_retry_count,
                "pre_ready_intents_garbage_collected": None,
                "log": str(cold_log),
            }

        host = start_host(temp_root, evidence_root, port=port, ordinal=2)
        if engine is None:
            engine_log = evidence_root / "machine-agent.log"
            engine_handle = engine_log.open("wb")
            engine_env = runtime_env(temp_root, engine_bin)
            engine = subprocess.Popen(
                [
                    str(engine_bin),
                    "connect",
                    "--url",
                    base_url,
                    "--token",
                    token,
                    "--db",
                    str(temp_root / "agent.db"),
                    "--machine-name",
                    DEVICE_ID,
                    "--fallback-scan-secs",
                    "1",
                    "--spool-replay-secs",
                    "1",
                ],
                env=engine_env,
                stdout=engine_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        if unready_intents:
            unready_ids = {
                str(intent.get("expected_session_id")) for intent in unready_intents
            }

            def pre_ready_intents_garbage_collected() -> bool:
                current_ids = {
                    str(intent.get("expected_session_id"))
                    for intent in read_retry_intents(temp_root)
                }
                return not (unready_ids & current_ids)

            wait_for(
                pre_ready_intents_garbage_collected,
                30,
                "pre-ready installed retry intents to be removed by the live Machine Agent",
            )
            if cold_restart_evidence is not None:
                cold_restart_evidence["pre_ready_intents_garbage_collected"] = len(
                    unready_ids
                )

        wait_for(
            lambda: read_retry_count(temp_root) == 0,
            60,
            "installed registration retry convergence after Runtime Host recovery",
        )
        last_launch_states: dict[str, str | None] = {}
        args._last_launch_states = last_launch_states

        def launches_adopted() -> bool:
            nonlocal last_launch_states
            last_launch_states = launch_attempt_states(
                runtime_host_live_database(temp_root), expected_session_ids
            )
            args._last_launch_states = last_launch_states
            ready_adopted = all(
                last_launch_states.get(session_id) == "adopted"
                for session_id in ready_session_ids
            )
            unready_absent = all(
                last_launch_states.get(session_id) is None
                for session_id in unready_session_ids
            )
            return bool(ready_session_ids) and ready_adopted and unready_absent

        def assert_unready_absent() -> None:
            unexpected_states = {
                session_id: state
                for session_id, state in last_launch_states.items()
                if session_id in unready_session_ids and state is not None
            }
            if unexpected_states:
                raise RuntimeError(
                    "pre-ready installed launches created host attempts after owner cleanup: "
                    f"{unexpected_states}"
                )

        if ready_session_ids:
            wait_for(
                launches_adopted,
                float(os.environ.get("LONGHOUSE_LAUNCH_STATE_TIMEOUT", "60")),
                "Runtime Host adoption of every recovered managed launch",
            )
            # Re-read after the ready adoption wait. This catches a delayed
            # pre-ready registration that appears after the main predicate
            # already observed all ready launches.
            last_launch_states = launch_attempt_states(
                runtime_host_live_database(temp_root), expected_session_ids
            )
            args._last_launch_states = last_launch_states
            assert_unready_absent()
        else:
            last_launch_states = launch_attempt_states(
                runtime_host_live_database(temp_root), expected_session_ids
            )
            args._last_launch_states = last_launch_states
            assert_unready_absent()
        for result, live in live_commands:
            cleanup = finish_live_command(live)
            if not cleanup["process_group_reaped"]:
                raise RuntimeError(
                    f"{result['provider']} provider process was not reaped after adoption"
                )
            result["returncode"] = cleanup["returncode"]
            result["detached_cleanup"] = {
                "status": "pass",
                "remaining_process_groups": [],
                **cleanup,
            }
        live_commands.clear()
        if engine_handle is not None:
            engine_handle.close()
            engine_handle = None
        return {
            "schema_version": 1,
            "artifact_kind": "installed_managed_launch_fault_matrix",
            "generated_at": utc_now(),
            "verdict": "yellow"
            if unready_intents or provider_failures
            else "green",
            "recovery_qualification": (
                "mixed_provider_degraded_start_with_harness_precondition_gap"
                if unready_intents and harness_precondition_failures
                else "mixed_provider_degraded_start_with_provider_owned_failure"
                if unready_intents and provider_failures
                else "degraded_start_only_provider_not_ready"
                if unready_intents
                else "harness_precondition_gap"
                if harness_precondition_failures
                else "provider_owned_start_failure"
                if provider_failures
                else "installed_provider_owner_and_host_adoption"
            ),
            "implementation": {
                "longhouse": str(longhouse_bin),
                "longhouse_engine": str(engine_bin),
                "longhouse_sha256": sha256_file(longhouse_bin),
                "engine_sha256": sha256_file(engine_bin),
            },
            "provider_binaries": provider_evidence,
            "scenarios": [
                "installed_provider_launch_while_runtime_host_unavailable",
                "durable_registration_retry_intent_per_provider",
                *(
                    ["machine_agent_registration_recovery_after_runtime_host_restart"]
                    if ready_session_ids
                    else ["machine_agent_restart_after_unqualified_degraded_start"]
                ),
                "installed_provider_exit_and_detach_cleanup",
                *(
                    ["concurrent_installed_provider_degraded_launch"]
                    if args.concurrent
                    else []
                ),
                *(
                    ["machine_agent_cold_restart_before_runtime_host_recovery"]
                    if args.cold_restart
                    else []
                ),
            ],
            "providers": results,
            "provider_startup_failures": provider_failures,
            "retry_intents_before_recovery": expected_retry_count,
            "retry_intents_after_recovery": 0,
            "machine_agent_cold_restart": cold_restart_evidence,
            "pre_ready_cleanup": pre_ready_cleanup_evidence,
            "launch_attempt_states": last_launch_states,
            "runtime_host_port": port,
            "machine_agent_log": str(engine_log) if engine_log else None,
            "evidence_root": str(evidence_root),
        }
    finally:
        for _, live in live_commands:
            try:
                finish_live_command(live)
            except Exception:
                pass
        live_commands.clear()
        if engine_handle is not None:
            engine_handle.close()
            engine_handle = None
        if engine is not None:
            kill_group(engine)
            try:
                engine.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        stop_host(host)
        stop_processes_for_root(temp_root)
        if os.environ.get("LONGHOUSE_KEEP_INSTALLED_FAULT_ROOT") != "1":
            shutil.rmtree(temp_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--longhouse-bin", type=Path)
    parser.add_argument("--engine-bin", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--provider", action="append", choices=PROVIDERS)
    parser.add_argument(
        "--concurrent",
        action="store_true",
        help="Run a second all-provider launch set concurrently.",
    )
    parser.add_argument(
        "--cold-restart",
        action="store_true",
        help="Restart a Machine Agent before Runtime Host recovery.",
    )
    parser.add_argument(
        "--allow-unqualified-recovery",
        action="store_true",
        help=(
            "Allow a yellow degraded-start-only result when an installed provider "
            "does not reach local readiness. This explicit qualification flag "
            "accepts yellow and returns exit code 0; without it yellow is nonzero."
        ),
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifact: dict[str, Any]
    try:
        artifact = run_matrix(args)
    except Exception as error:  # noqa: BLE001 - qualification must emit evidence on every failure.
        artifact = {
            "schema_version": 1,
            "artifact_kind": "installed_managed_launch_fault_matrix",
            "generated_at": utc_now(),
            "verdict": "red",
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
            "providers": getattr(args, "_matrix_results", []),
            "launch_attempt_states": getattr(args, "_last_launch_states", {}),
            "retry_intents": getattr(args, "_retry_intents", []),
            "evidence_root": str(getattr(args, "_resolved_evidence_root", "")),
            "temporary_root": str(getattr(args, "_temporary_root", "")),
        }
    evidence_root = Path(
        artifact.get("evidence_root")
        or getattr(args, "_resolved_evidence_root", None)
        or args.evidence_root
        or ".build/canaries/installed-managed-launch-fault-matrix"
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    artifact_path = evidence_root / "installed-managed-launch-fault-matrix.json"
    artifact["artifact_path"] = str(artifact_path.resolve())
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"verdict: {artifact['verdict']}")
        print(f"artifact: {artifact_path}")
        if artifact["verdict"] != "green":
            print(
                artifact.get("error", "installed fault matrix failed"), file=sys.stderr
            )
    return 0 if artifact["verdict"] == "green" or (
        artifact["verdict"] == "yellow" and args.allow_unqualified_recovery
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
