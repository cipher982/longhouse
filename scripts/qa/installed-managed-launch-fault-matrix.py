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
import errno
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
        live_command: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.output = output
        self.timed_out = timed_out
        self.live_command = live_command


class ProcessScanFailure(RuntimeError):
    """The bounded cleanup observer could not prove process absence."""


@dataclass
class LiveCommand:
    process: Any
    output_fd: int
    output: bytearray
    is_tty: bool
    provider_ready_observed: bool


class ForkedProcess:
    """Small Popen-compatible owner for a ``pty.fork`` child."""

    def __init__(self, pid: int, args: list[str]) -> None:
        self.pid = pid
        self.args = args
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = -signal.SIGKILL
            return self.returncode
        if waited_pid == self.pid:
            self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = self.poll()
            if status is not None:
                return status
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.1)

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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


def _binary_build_identity(
    path: Path, *, environment: dict[str, str] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = subprocess.run(
            [str(path), "build-identity", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"build identity probe failed: {type(error).__name__}: {error}"
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or "no output").strip()[-500:]
        return None, f"build identity probe exited {result.returncode}: {detail}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return None, f"build identity probe returned invalid JSON: {error}"
    if not isinstance(payload, dict):
        return None, "build identity probe returned a non-object JSON value"
    identity = (
        payload.get("facade") if isinstance(payload.get("facade"), dict) else payload
    )
    commit = identity.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None, "build identity probe returned an invalid commit"
    if not isinstance(identity.get("dirty"), bool):
        return None, "build identity probe returned an invalid dirty flag"
    return payload, None


def source_provenance(
    path: Path, *, environment: dict[str, str] | None = None
) -> dict[str, Any]:
    """Bind an installed binary to the identity compiled into that binary."""

    path = path.resolve()
    identity, identity_error = _binary_build_identity(path, environment=environment)
    reported_identity = (
        identity.get("facade")
        if isinstance(identity, dict) and isinstance(identity.get("facade"), dict)
        else identity
    )
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "source_worktree": None,
        "source_git_sha": (
            reported_identity.get("commit")
            if isinstance(reported_identity, dict)
            else None
        ),
        "source_dirty": (
            reported_identity.get("dirty")
            if isinstance(reported_identity, dict)
            else None
        ),
        "build_identity": identity,
        "build_identity_error": identity_error,
    }


def implementation_snapshot(
    longhouse_bin: Path, engine_bin: Path
) -> dict[str, dict[str, Any]]:
    probe_env = os.environ.copy()
    probe_env["LONGHOUSE_ENGINE_BIN"] = str(engine_bin)
    return {
        "longhouse": source_provenance(longhouse_bin, environment=probe_env),
        "longhouse_engine": source_provenance(engine_bin),
    }


def validated_implementation_snapshot(
    longhouse_bin: Path, engine_bin: Path
) -> dict[str, dict[str, Any]]:
    snapshot = implementation_snapshot(longhouse_bin, engine_bin)
    for label in ("longhouse", "longhouse_engine"):
        value = snapshot[label]
        if value.get("build_identity_error") is not None:
            raise RuntimeError(
                f"{label} build identity could not be verified: "
                f"{value['build_identity_error']}"
            )
        if value.get("source_dirty") is not False:
            raise RuntimeError(f"{label} build identity is dirty")
        if not isinstance(value.get("source_git_sha"), str):
            raise RuntimeError(f"{label} build identity has no source commit")
        if not re.fullmatch(r"[0-9a-f]{40}", value["source_git_sha"]):
            raise RuntimeError(f"{label} build identity has an invalid source commit")
        if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256"))):
            raise RuntimeError(f"{label} executable hash is invalid")

    longhouse_identity = snapshot["longhouse"].get("build_identity")
    engine_identity = snapshot["longhouse_engine"].get("build_identity")
    if not isinstance(longhouse_identity, dict) or not isinstance(
        longhouse_identity.get("engine"), dict
    ):
        raise RuntimeError("longhouse build identity has no paired engine record")
    paired_engine_path = longhouse_identity.get("engine_path")
    if not isinstance(paired_engine_path, str) or not paired_engine_path:
        raise RuntimeError("longhouse build identity has no paired engine path")
    if Path(paired_engine_path).expanduser().resolve() != engine_bin.resolve():
        raise RuntimeError(
            "longhouse build identity paired engine path does not match the tested engine"
        )
    paired_engine = longhouse_identity["engine"]
    if (
        paired_engine.get("commit") != snapshot["longhouse_engine"]["source_git_sha"]
        or paired_engine.get("dirty") is not False
        or not isinstance(engine_identity, dict)
        or engine_identity.get("commit") != paired_engine.get("commit")
        or engine_identity.get("dirty") is not False
    ):
        raise RuntimeError("longhouse facade and engine build identities disagree")
    if (
        snapshot["longhouse"]["source_git_sha"]
        != snapshot["longhouse_engine"]["source_git_sha"]
    ):
        raise RuntimeError("longhouse facade and engine source commits disagree")
    return snapshot


def assert_implementation_stable(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> None:
    for label in ("longhouse", "longhouse_engine"):
        if before[label].get("sha256") != after[label].get("sha256"):
            raise RuntimeError(
                f"{label} executable changed during the qualification run"
            )


def harness_provenance() -> dict[str, Any]:
    """Bind evidence to the harness and Runtime Host checkout."""

    path = Path(__file__).resolve()
    repository = path.parents[2]
    revision = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    relative_path = str(path.relative_to(repository))
    dirty = subprocess.run(
        ["git", "-C", str(repository), "status", "--short", "--", relative_path],
        capture_output=True,
        text=True,
        check=False,
    )
    repository_status = subprocess.run(
        ["git", "-C", str(repository), "status", "--short"],
        capture_output=True,
        text=True,
        check=False,
    )
    repository_git_sha = revision.stdout.strip() if revision.returncode == 0 else None
    harness_file_dirty = bool(dirty.stdout.strip()) if dirty.returncode == 0 else None
    repository_dirty = (
        bool(repository_status.stdout.strip())
        if repository_status.returncode == 0
        else None
    )
    return {
        "path": str(path),
        "git_sha": repository_git_sha,
        "repository": str(repository),
        "repository_git_sha": repository_git_sha,
        "repository_dirty": repository_dirty,
        "sha256": sha256_file(path),
        "argv": list(sys.argv),
        "cwd": os.getcwd(),
        "dirty": repository_dirty,
        "harness_file_dirty": harness_file_dirty,
    }


def verified_harness_provenance() -> dict[str, Any]:
    """Return usable repository provenance or fail closed before qualification."""
    provenance = harness_provenance()
    missing = [
        key
        for key in ("repository_git_sha", "repository_dirty", "harness_file_dirty")
        if provenance.get(key) is None
    ]
    if missing:
        raise ProcessScanFailure(
            "harness provenance could not be verified: " + ", ".join(missing)
        )
    return provenance


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


def remove_temporary_root(temp_root: Path) -> None:
    """Remove the disposable credential root or fail closed."""
    try:
        shutil.rmtree(temp_root)
    except Exception as error:  # noqa: BLE001 - cleanup must be reported.
        raise ProcessScanFailure(
            f"temporary-root deletion failed for {temp_root}: {error}"
        ) from error
    if temp_root.exists():
        raise ProcessScanFailure(
            f"temporary-root deletion left the path present: {temp_root}"
        )


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

    def matching_processes() -> dict[int, str]:
        try:
            listing = subprocess.run(
                ["ps", "-Ao", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProcessScanFailure(f"ps root-process scan failed: {error}") from error
        if listing.returncode != 0:
            raise ProcessScanFailure(
                "ps root-process scan failed: "
                f"returncode={listing.returncode} stderr={listing.stderr.strip()}"
            )
        processes: dict[int, str] = {}
        for line in listing.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or needle not in fields[1]:
                continue
            try:
                pid = int(fields[0])
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            identity = process_start_identity(pid)
            if identity is not None:
                processes[pid] = identity
        return processes

    for _ in range(2):
        processes = matching_processes()
        for pid, identity in processes.items():
            # The host-wide match is only a candidate. Re-check the exact
            # start identity immediately before signalling its group.
            if process_start_identity(pid) == identity:
                kill_group(pid, grace=0.1)
        if processes:
            time.sleep(0.2)
    remaining = matching_processes()
    if remaining:
        raise ProcessScanFailure(
            f"detached Runtime Host workers remain after cleanup: {sorted(remaining)}"
        )


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


def session_process_groups(session_id: str) -> dict[int, dict[int, str]]:
    """Find session-bearing groups and record identities before signalling."""

    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,pgid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessScanFailure(f"ps session-group scan failed: {error}") from error
    if listing.returncode != 0:
        raise ProcessScanFailure(
            "ps session-group scan failed: "
            f"returncode={listing.returncode} stderr={listing.stderr.strip()}"
        )
    groups: dict[int, dict[int, str]] = {}
    for line in listing.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or session_id not in fields[2]:
            continue
        try:
            pid = int(fields[0])
            pgid = int(fields[1])
        except ValueError:
            continue
        if pid == os.getpid() or pgid == os.getpgrp():
            continue
        identity = process_start_identity(pid)
        if identity is not None:
            groups.setdefault(pgid, {})[pid] = identity
    return groups


def verified_session_groups(groups: dict[int, dict[int, str]]) -> set[int]:
    """Return only groups with an identity-verified live session member."""

    verified: set[int] = set()
    for pgid, members in groups.items():
        if any(
            process_start_identity(pid) == identity for pid, identity in members.items()
        ):
            verified.add(pgid)
    return verified


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
    natural_timeout = float(os.environ.get("LONGHOUSE_NATURAL_CLEANUP_TIMEOUT", "8"))
    natural_deadline = time.monotonic() + natural_timeout
    remaining: list[int] = []
    while time.monotonic() < natural_deadline:
        remaining = sorted(verified_session_groups(session_process_groups(session_id)))
        if not remaining:
            break
        time.sleep(0.1)
    remaining = sorted(verified_session_groups(session_process_groups(session_id)))
    forced_cleanup = bool(remaining)
    if forced_cleanup:
        for pgid in remaining:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (OSError, PermissionError):
                pass
        time.sleep(0.5)
        remaining = sorted(verified_session_groups(session_process_groups(session_id)))
        for pgid in remaining:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, PermissionError):
                pass
        time.sleep(0.2)
    final_groups = session_process_groups(session_id)
    final_group_ids = sorted(verified_session_groups(final_groups))
    return {
        "status": (
            "forced_cleanup"
            if forced_cleanup
            else "pass"
            if not final_group_ids
            else "fail"
        ),
        "stop_returncode": stop.returncode,
        "stop_output": redact(
            (stop.stdout or "") + (stop.stderr or ""),
            env.get("LONGHOUSE_DEVICE_TOKEN", ""),
        ),
        "natural_cleanup_observed": not forced_cleanup,
        "forced_cleanup": forced_cleanup,
        "remaining_process_groups": final_group_ids,
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


def drain_pty_after_exit(master: int, output: bytearray) -> None:
    """Drain bytes already queued after a fast child exit without blocking."""

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            readable, _, _ = select.select([master], [], [], min(0.05, remaining))
        except (OSError, ValueError):
            return
        if not readable:
            return
        try:
            chunk = os.read(master, 8192)
        except OSError as error:
            if error.errno == errno.EIO:
                return
            return
        if not chunk:
            return
        output.extend(chunk)


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
    returncode: int | None = None
    pty_closed = False
    try:
        while time.monotonic() - started < timeout:
            readable, _, _ = select.select([] if pty_closed else [master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master, 8192)
                    if chunk:
                        output.extend(chunk)
                    else:
                        pty_closed = True
                except OSError as error:
                    if error.errno == errno.EIO:
                        pty_closed = True
                    else:
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
            returncode = wait_status(pid, 0.0)
            if returncode is not None:
                break
        timed_out = returncode is None
        if timed_out:
            kill_group(pid, grace=0.2)
            returncode = wait_status(pid, 5)
        elif not pty_closed:
            drain_pty_after_exit(master, output)
            decoded = output.decode("utf-8", errors="replace")
            if marker in decoded:
                marker_seen = True
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

    ``pty.fork`` gives the child a real session and controlling terminal,
    matching the user's terminal contract. The forked child execs immediately
    and the parent owns its exact PID with ``waitpid``. The returned process
    remains alive until Runtime Host recovery has been asserted, so the
    registration retry cannot silently settle as a post-hoc provider abort.
    """

    if use_tty:
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(env.get("LONGHOUSE_FAULT_CWD", "."))
                os.execvpe(command[0], command, env)
            except BaseException:
                os._exit(127)
        process = ForkedProcess(pid, command)
        # Later provider launches are forked from this harness process. Do not
        # let those children inherit an earlier provider's pty master; the
        # parent must be able to close one terminal and deliver its HUP/EOF
        # independently during exact cleanup.
        os.set_inheritable(master_fd, False)
        os.set_blocking(master_fd, False)
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
                    live_command=live,
                )
        raise ProviderLaunchError(
            f"provider did not emit startup marker within {timeout}s",
            returncode=process.poll(),
            output=live.output.decode("utf-8", errors="replace"),
            timed_out=True,
            live_command=live,
        )
    except ProviderLaunchError:
        # The caller owns cleanup for a bounded startup failure. Keeping the
        # live handle attached to the error lets it prove the exact launcher
        # and provider tree was reaped instead of losing the process at the
        # exception boundary.
        raise
    except BaseException:
        # Unexpected harness errors still need local cleanup. Provider launch
        # failures deliberately take the path above so their cleanup evidence
        # is returned in the provider result.
        if process.poll() is None:
            kill_group(process, grace=0.2)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if use_tty:
            try:
                os.close(output_fd)
            except OSError:
                pass
        elif process.stdout is not None:
            process.stdout.close()
        raise


def process_start_identity(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessScanFailure(f"ps process-identity scan failed: {error}") from error
    # macOS ps returns 1 when a process disappears between observations. That
    # is a valid absence result; any other nonzero status is an observer error.
    if result.returncode not in {0, 1}:
        raise ProcessScanFailure(
            "ps process-identity scan failed: "
            f"returncode={result.returncode} stderr={result.stderr.strip()}"
        )
    value = result.stdout.strip()
    return value or None


def process_records(pids: set[int]) -> dict[int, dict[str, Any]]:
    """Read a bounded set of process records without scanning the host."""

    pids = {pid for pid in pids if pid != os.getpid()}
    if not pids:
        return {}
    try:
        listing = subprocess.run(
            [
                "ps",
                "-p",
                ",".join(str(pid) for pid in sorted(pids)),
                "-o",
                "pid=,ppid=,pgid=,stat=,command=",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessScanFailure(f"ps process-record scan failed: {error}") from error
    if listing.returncode not in {0, 1}:
        raise ProcessScanFailure(
            "ps process-record scan failed: "
            f"returncode={listing.returncode} stderr={listing.stderr.strip()}"
        )
    records: dict[int, dict[str, Any]] = {}
    for line in listing.stdout.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) != 5:
            continue
        try:
            pid, ppid, pgid = (int(value) for value in fields[:3])
        except ValueError:
            continue
        records[pid] = {
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "stat": fields[3],
            "command": fields[4],
        }
    return records


def process_group_pids(pgid: int) -> set[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-g", str(pgid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessScanFailure(f"pgrep process-group scan failed: {error}") from error
    # pgrep returns 1 for a valid empty group and >1 for an observer failure.
    if result.returncode not in {0, 1}:
        raise ProcessScanFailure(
            "pgrep process-group scan failed: "
            f"returncode={result.returncode} stderr={result.stderr.strip()}"
        )
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            continue
    return pids


def scoped_process_table(scope: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Find only the provider tree owned by one launch attempt.

    The disposable root also contains the Runtime Host and Machine Agent, so
    scanning that root and killing every match is unsafe. Exact recorded PIDs
    and their recorded process groups form this scoped cleanup boundary; no
    host-wide process or open-file scan is part of this scoped decision.
    """

    selected: set[int] = set()
    exact_identities = {
        int(scope[key]): scope.get(identity_key)
        for key, identity_key in (
            ("launcher_pid", "launcher_start_identity"),
            ("provider_pid", "provider_process_start_time"),
        )
        if scope.get(key)
    }
    for pid, start_identity in exact_identities.items():
        if start_identity is None or process_start_identity(pid) == start_identity:
            selected.add(pid)

    process_groups = {
        int(scope[key]) for key in ("launcher_pgid", "provider_pgid") if scope.get(key)
    }
    # A provider can be reparented after the facade returns, but its process
    # group remains the exact ownership boundary. Read only those groups and
    # exact PIDs; a host-wide ps scan is both slow and prone to unrelated
    # agent churn on a developer machine.
    try:
        for pgid in process_groups:
            selected.update(process_group_pids(pgid))
        return process_records(selected)
    except ProcessScanFailure as error:
        # Never turn an observer timeout into an empty/green cleanup result.
        # A synthetic record keeps the evidence bounded and forces the matrix
        # red until the process boundary can actually be observed.
        return {
            -1: {
                "pid": -1,
                "ppid": 0,
                "pgid": -1,
                "stat": "scan_error",
                "command": str(error),
            }
        }


def public_process_records(records: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: record[key] for key in ("pid", "ppid", "pgid", "stat")}
        for record in records.values()
    ]


def verified_cleanup_groups(
    records: dict[int, dict[str, Any]], scope: dict[str, Any]
) -> set[int]:
    """Return only groups whose recorded owner identity is still present."""

    candidate_groups = {
        int(record["pgid"])
        for record in records.values()
        if int(record["pgid"]) > 0 and int(record["pgid"]) != os.getpgrp()
    }
    verified: set[int] = set()
    for pid_key, start_key, group_key in (
        ("launcher_pid", "launcher_start_identity", "launcher_pgid"),
        ("provider_pid", "provider_process_start_time", "provider_pgid"),
    ):
        pid = scope.get(pid_key)
        start_identity = scope.get(start_key)
        pgid = scope.get(group_key)
        if (
            not pid
            or not start_identity
            or not pgid
            or int(pgid) not in candidate_groups
        ):
            continue
        if process_start_identity(int(pid)) == start_identity:
            verified.add(int(pgid))
    return verified


def finish_live_command(
    command: LiveCommand,
    *,
    provider_pid: int | None = None,
    provider_process_start_time: str | None = None,
    cleanup_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stop one exact launcher/provider process group and assert it is reaped.

    The Longhouse facade and the upstream provider use separate process groups
    after terminal handoff.  Killing only the facade leaves a provider child
    alive and can make the facade wait forever.  The provider PID comes from
    the durable retry intent and is checked against its recorded start
    identity before any signal is sent.
    """

    provider_kill_status = "not_observed"
    provider_cleanup_pid: int | None = None
    provider_natural_cleanup_verified = False
    if (
        provider_pid is not None
        and provider_process_start_time is not None
        and process_start_identity(provider_pid) == provider_process_start_time
    ):
        provider_cleanup_pid = provider_pid
    elif provider_pid is not None:
        provider_kill_status = (
            "identity_missing"
            if provider_process_start_time is None
            else "identity_mismatch"
        )
    elif (
        cleanup_scope is not None
        and cleanup_scope.get("provider_process_observed") is False
    ):
        provider_kill_status = "not_started"

    if command.is_tty:
        try:
            os.write(command.output_fd, b"\x03")
        except OSError:
            pass
    # Closing the owned pty is part of terminating a TTY provider. Keeping the
    # master open can leave the launcher/provider group in macOS `?E` state
    # during natural-stop observation.
    if command.is_tty:
        try:
            os.close(command.output_fd)
        except OSError:
            pass
    elif command.process.stdout is not None:
        command.process.stdout.close()

    # First observe the provider's own bounded stop path. The qualification
    # must prove that Longhouse can end a managed session without the harness
    # rescuing it; forced signals are evidence of a product cleanup failure,
    # even if the final process scan is empty.
    natural_cleanup_timeout = float(
        os.environ.get("LONGHOUSE_NATURAL_CLEANUP_TIMEOUT", "8")
    )
    natural_deadline = time.monotonic() + natural_cleanup_timeout
    natural_remaining: dict[int, dict[str, Any]] = {}
    while time.monotonic() < natural_deadline:
        command.process.poll()
        natural_remaining = (
            scoped_process_table(cleanup_scope) if cleanup_scope is not None else {}
        )
        if command.process.poll() is not None and not natural_remaining:
            break
        time.sleep(0.1)
    command.process.poll()
    natural_remaining = (
        scoped_process_table(cleanup_scope) if cleanup_scope is not None else {}
    )
    launcher_reaped_naturally = command.process.poll() is not None
    forced_cleanup = not launcher_reaped_naturally or bool(natural_remaining)
    if forced_cleanup and command.process.poll() is None:
        kill_group(command.process, grace=0.2)
    if provider_cleanup_pid is not None:
        if forced_cleanup:
            kill_group(provider_cleanup_pid, grace=0.2)
            provider_kill_status = "attempted"
        else:
            provider_group_proof_available = bool(
                cleanup_scope is not None and cleanup_scope.get("provider_pgid")
            )
            if (
                provider_group_proof_available
                and process_start_identity(provider_cleanup_pid) is None
            ):
                provider_kill_status = "natural"
                provider_natural_cleanup_verified = True
            else:
                provider_kill_status = (
                    "natural_unscoped"
                    if not provider_group_proof_available
                    else "natural_unverified"
                )

    if cleanup_scope is not None:
        # Re-scan after the normal signals. Detached provider bridges are
        # allowed to outlive the facade, so terminate only the attributable
        # scoped records and verify the boundary again.
        remaining = scoped_process_table(cleanup_scope)
        if forced_cleanup:
            for pgid in sorted(verified_cleanup_groups(remaining, cleanup_scope)):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (OSError, PermissionError):
                    pass
            time.sleep(0.5)
            remaining = scoped_process_table(cleanup_scope)
            for pgid in sorted(verified_cleanup_groups(remaining, cleanup_scope)):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (OSError, PermissionError):
                    pass
        cleanup_timeout = float(
            os.environ.get("LONGHOUSE_INSTALLED_CLEANUP_TIMEOUT", "30")
        )
        deadline = time.monotonic() + cleanup_timeout
        while time.monotonic() < deadline:
            # macOS can expose a killed pty child as `?E` for a short interval
            # before it becomes waitable. Retry the parent wait on every scan
            # so the harness owns the zombie instead of leaving it to launchd.
            command.process.poll()
            remaining = scoped_process_table(cleanup_scope)
            if command.process.poll() is not None and not remaining:
                break
            time.sleep(0.1)
        # Give the final wait/ps observation a short convergence tail. On
        # macOS the `?E` record can become a zombie between the last scan and
        # the waitpid call; one more poll must reap it before evidence is
        # emitted.
        for _ in range(20):
            remaining = scoped_process_table(cleanup_scope)
            command.process.poll()
            remaining = scoped_process_table(cleanup_scope)
            if command.process.poll() is not None and not remaining:
                break
            time.sleep(0.1)
        command.process.poll()
        remaining = scoped_process_table(cleanup_scope)
    else:
        remaining = {}
    # The scoped sweep can be the operation that finishes a detached pty
    # launcher. Re-read the child status after that sweep; otherwise a process
    # that disappeared during the bounded cleanup window is reported as a
    # false leak.
    launcher_reaped = command.process.poll() is not None
    launcher_absent_after_cleanup = not any(
        record.get("pid") == command.process.pid for record in remaining.values()
    )
    remaining_process_groups = sorted(
        {
            int(record["pgid"])
            for record in remaining.values()
            if int(record["pgid"]) > 0 and int(record["pgid"]) != os.getpgrp()
        }
    )
    cleanup_status = (
        "forced_cleanup"
        if forced_cleanup
        else "pass"
        if (
            launcher_absent_after_cleanup
            and not remaining
            and provider_pid is not None
            and provider_natural_cleanup_verified
        )
        else "not_started"
        if (
            launcher_absent_after_cleanup
            and not remaining
            and cleanup_scope is not None
            and cleanup_scope.get("provider_process_observed") is False
        )
        else "fail"
    )
    return {
        "returncode": command.process.returncode,
        "process_group_reaped": launcher_reaped or launcher_absent_after_cleanup,
        "launcher_reaped_naturally": launcher_reaped_naturally,
        "natural_cleanup_observed": not forced_cleanup,
        "launcher_wait_reaped": launcher_reaped,
        "launcher_absent_after_cleanup": launcher_absent_after_cleanup,
        "provider_pid": provider_pid,
        "provider_process_group_cleanup": provider_kill_status,
        "forced_launcher_cleanup": not launcher_reaped_naturally,
        "forced_cleanup": forced_cleanup,
        "remaining_processes": public_process_records(remaining),
        "remaining_process_groups": remaining_process_groups,
        "status": cleanup_status,
    }


def provider_command(
    provider: str,
    *,
    longhouse_bin: Path,
    provider_bin: Path,
    root: Path,
    profile_name: str | None = None,
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
    provider_config_root = root / "provider-config" / (profile_name or provider)
    if provider == "claude":
        command.extend(["--claude-dir", str(provider_config_root)])
    elif provider == "opencode":
        if not keep_attached:
            command.append("--no-attach")
        command.extend(["--claude-dir", str(provider_config_root)])
    elif provider == "codex":
        if not keep_attached:
            command.append("--no-attach")
    else:
        command.extend(
            [
                "--config-dir",
                str(provider_config_root),
                "--permission-mode",
                "auto_approve",
                "--verbose",
            ]
        )
    return command


def provider_cleanup_scope(
    *,
    profile_root: Path,
    provider_home: Path,
    cwd: Path,
    launcher_pid: int | None,
    launcher_start_identity: str | None,
    provider_pid: int | None,
    provider_process_start_time: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    def group_for(pid: int | None) -> int | None:
        if pid is None:
            return None
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None

    return {
        "launcher_pid": launcher_pid,
        "launcher_start_identity": launcher_start_identity,
        "launcher_pgid": group_for(launcher_pid),
        "provider_pid": provider_pid,
        "provider_process_start_time": provider_process_start_time,
        "provider_pgid": group_for(provider_pid),
        "session_id": session_id,
        "provider_config_root": str(profile_root),
        "provider_home": str(provider_home),
        "provider_cwd": str(cwd),
    }


def observed_provider_record(
    scope: dict[str, Any], provider_bin: Path
) -> dict[str, Any] | None:
    """Find a provider child in the launcher's exact process group, if any."""

    records = scoped_process_table(scope)
    tokens = {str(provider_bin), provider_bin.name}
    candidates = [
        record
        for record in records.values()
        if int(record.get("pid", 0)) > 0
        and int(record.get("pid", 0)) != int(scope.get("launcher_pid") or -1)
        and any(token and token in str(record.get("command", "")) for token in tokens)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda record: int(record["pid"]))[-1]


def same_path(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def retry_intent_matches_launch(
    intent: dict[str, Any],
    *,
    provider: str,
    launcher_pid: int | None,
    provider_cwd: Path,
    session_id: str | None = None,
) -> bool:
    if str(intent.get("provider_name", "")).lower() != provider.lower():
        return False
    if session_id and str(intent.get("expected_session_id")) == session_id:
        return True
    if launcher_pid is not None and intent.get("launcher_pid") == launcher_pid:
        return True
    payload = intent.get("payload")
    return isinstance(payload, dict) and same_path(payload.get("cwd"), provider_cwd)


def retry_owner_ready(
    root: Path,
    provider: str,
    launcher_pid: int,
    provider_cwd: Path,
) -> bool:
    directory = root / "longhouse" / "agent" / "managed-local" / "registration-retries"
    for path in directory.glob("*.json"):
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if intent.get("provider_ready") is True and retry_intent_matches_launch(
            intent,
            provider=provider,
            launcher_pid=launcher_pid,
            provider_cwd=provider_cwd,
        ):
            return True
    return False


def provider_failure_qualification(provider: str, output: str) -> str:
    # These provider-native probes can fail before Longhouse gets a readiness
    # signal when an isolated qualification profile lacks the provider's own
    # account/session state. Do not mislabel that harness precondition failure
    # as a Longhouse startup defect.
    if provider == "claude" and (
        "auth status" in output.lower()
        or "native channels unavailable" in output.lower()
    ):
        return "harness_precondition_unmet"
    return "provider_owned_start_failure"


def provider_native_output_observed(provider: str, output: str) -> bool:
    if provider != "cursor":
        return bool(output.strip())
    harness_prefixes = (
        "Longhouse ",
        "Managed Cursor ",
        "Timeline: ",
    )
    return any(
        line.strip() and not line.startswith(harness_prefixes)
        for line in output.splitlines()
    )


def bootstrap_provider_auth(
    provider: str,
    *,
    provider_bin: Path,
    root: Path,
) -> dict[str, Any]:
    """Prepare provider-native auth in the disposable qualification root.

    Claude, Cursor, and OpenCode consume request-scoped environment
    credentials. Stock Codex does not: its supported isolated path is
    ``codex login --with-api-key`` with the key supplied on stdin. The matrix
    never copies a daily provider profile and records only the auth method,
    never credential material.
    """

    if provider == "claude":
        configured = bool(
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_CONFIG_DIR")
        )
        return {
            "status": "ready" if configured else "missing",
            "method": "provider_environment_or_explicit_config_dir",
        }
    if provider == "cursor":
        api_key_configured = bool(os.environ.get("CURSOR_API_KEY"))
        profile_root = root / "provider-home" / provider
        (root / "cwd" / provider).mkdir(mode=0o700, parents=True, exist_ok=True)
        profile_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile_env = os.environ.copy()
        profile_env.update(
            {
                "HOME": str(profile_root),
                "XDG_CONFIG_HOME": str(profile_root / "config"),
                "XDG_DATA_HOME": str(profile_root / "data"),
                "XDG_STATE_HOME": str(profile_root / "state"),
                "XDG_CACHE_HOME": str(profile_root / "cache"),
            }
        )
        status_probe_timed_out = False
        status_probe_returncode: int | None = None
        provider_status: str | None = None
        account_authenticated = False
        try:
            status_probe = subprocess.run(
                [str(provider_bin), "status", "--format", "json"],
                cwd=str(root / "cwd" / provider),
                env=profile_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            status_probe_returncode = status_probe.returncode
            try:
                status_payload = json.loads(status_probe.stdout or "{}")
            except json.JSONDecodeError:
                status_payload = {}
            if isinstance(status_payload, dict):
                provider_status = (
                    str(status_payload["status"])
                    if status_payload.get("status") is not None
                    else None
                )
                account_authenticated = status_payload.get("isAuthenticated") is True
        except subprocess.TimeoutExpired:
            status_probe_timed_out = True
        return {
            # Cursor's API key is not sufficient for ``create-chat`` in a
            # fresh profile. The installed lane must report that account
            # prerequisite explicitly instead of calling the Longhouse launch
            # path broken after its bounded provider wait expires.
            "status": "ready" if account_authenticated else "missing",
            "method": "cursor_account_session_and_api_key",
            "api_key_configured": api_key_configured,
            "account_session_authenticated": account_authenticated,
            "provider_status": provider_status,
            "status_probe_returncode": status_probe_returncode,
            "status_probe_timed_out": status_probe_timed_out,
            "precondition_failure": (
                "cursor_status_probe_timeout"
                if status_probe_timed_out
                else "cursor_account_session_not_authenticated"
                if not account_authenticated
                else None
            ),
        }
    if provider == "opencode":
        configured = bool(
            os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        return {
            "status": "ready" if configured else "provider_local_or_missing",
            "method": "provider_environment",
        }

    api_key = (
        os.environ.get("CODEX_API_KEY")
        or os.environ.get("LONGHOUSE_CI_CODEX_API_KEY")
        or ""
    ).strip()
    codex_home = root / "provider-config" / "codex"
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "cwd" / provider).mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "provider-home" / provider).mkdir(mode=0o700, parents=True, exist_ok=True)
    if not api_key:
        return {
            "status": "missing",
            "method": "codex_login_with_api_key_stdin",
            "auth_path": str(codex_home / "auth.json"),
        }
    env = os.environ.copy()
    env.update(
        {
            "CODEX_HOME": str(codex_home),
            "HOME": str(root / "provider-home" / provider),
        }
    )
    env.pop("CODEX_API_KEY", None)
    result = subprocess.run(
        [str(provider_bin), "login", "--with-api-key"],
        cwd=str(root / "cwd" / provider),
        env=env,
        input=f"{api_key}\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    detail = (
        (result.stderr or result.stdout or "").replace(api_key, "[REDACTED]").strip()
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Codex isolated login failed ({result.returncode}): {detail[:240]}"
        )
    auth_path = codex_home / "auth.json"
    if not auth_path.is_file():
        raise RuntimeError("Codex isolated login completed without auth.json")
    auth_path.chmod(0o600)
    return {
        "status": "ready",
        "method": "codex_login_with_api_key_stdin",
        "auth_path": str(auth_path),
        "provider_output": detail[:240],
    }


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
    profile_name: str | None = None,
) -> tuple[dict[str, Any], LiveCommand | None]:
    profile_name = profile_name or provider
    provider_root = evidence_root / "providers" / provider
    provider_root.mkdir(parents=True, exist_ok=True)
    # Concurrent qualification profiles need an isolated cwd as well as an
    # isolated provider config/home. The cwd is part of the cleanup identity;
    # sharing it would make one failed Cursor teardown select the other Cursor
    # launch tree.
    cwd = root / "cwd" / profile_name
    cwd.mkdir(parents=True, exist_ok=True)
    env = runtime_env(root, engine_bin)
    env["LONGHOUSE_FAULT_CWD"] = str(cwd)
    env["CODEX_HOME"] = str(root / "provider-config" / profile_name)
    provider_home = root / "provider-home" / profile_name
    env["HOME"] = str(provider_home)
    env["XDG_CONFIG_HOME"] = str(provider_home / "config")
    env["XDG_DATA_HOME"] = str(provider_home / "data")
    env["XDG_STATE_HOME"] = str(provider_home / "state")
    env["XDG_CACHE_HOME"] = str(provider_home / "cache")
    provider_config_root = root / "provider-config" / profile_name
    if provider == "codex":
        codex_home = provider_config_root
        codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        source_auth = root / "provider-config" / "codex" / "auth.json"
        target_auth = codex_home / "auth.json"
        if (
            profile_name != provider
            and source_auth.is_file()
            and not target_auth.exists()
        ):
            shutil.copyfile(source_auth, target_auth)
            target_auth.chmod(0o600)
    command = provider_command(
        provider,
        longhouse_bin=longhouse_bin,
        provider_bin=provider_bin,
        root=root,
        profile_name=profile_name,
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
                root, provider, launcher_pid, cwd
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
        live = error.live_command
        launcher_pid = live.process.pid if live is not None else None
        owner = next(
            (
                intent
                for intent in read_retry_intents(root)
                if retry_intent_matches_launch(
                    intent,
                    provider=provider,
                    launcher_pid=launcher_pid,
                    provider_cwd=cwd,
                    session_id=session_match.group(0) if session_match else None,
                )
            ),
            None,
        )
        if owner is not None:
            launch_intent_created = True
        provider_pid = owner.get("provider_pid") if owner else None
        provider_start = owner.get("provider_process_start_time") if owner else None
        launcher_start = (
            process_start_identity(launcher_pid) if launcher_pid is not None else None
        )
        cleanup_scope = provider_cleanup_scope(
            profile_root=provider_config_root,
            provider_home=provider_home,
            cwd=cwd,
            launcher_pid=launcher_pid,
            launcher_start_identity=launcher_start,
            provider_pid=provider_pid,
            provider_process_start_time=provider_start,
            session_id=(
                str(owner.get("expected_session_id"))
                if owner and owner.get("expected_session_id")
                else session_match.group(0)
                if session_match
                else None
            ),
        )
        observed = None
        if provider_pid is None:
            observed = observed_provider_record(cleanup_scope, provider_bin)
            if observed is not None:
                provider_pid = int(observed["pid"])
                provider_start = process_start_identity(provider_pid)
                cleanup_scope = provider_cleanup_scope(
                    profile_root=provider_config_root,
                    provider_home=provider_home,
                    cwd=cwd,
                    launcher_pid=launcher_pid,
                    launcher_start_identity=launcher_start,
                    provider_pid=provider_pid,
                    provider_process_start_time=provider_start,
                    session_id=(
                        str(owner.get("expected_session_id"))
                        if owner and owner.get("expected_session_id")
                        else session_match.group(0)
                        if session_match
                        else None
                    ),
                )
        cleanup_scope["provider_process_observed"] = (
            provider_pid is not None or observed is not None
        )
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
                "launcher_pid": launcher_pid,
                "launcher_start_identity": launcher_start,
                "provider_pid": provider_pid,
                "provider_process_start_time": provider_start,
                "provider_config_root": str(provider_config_root),
                "provider_home": str(provider_home),
                "provider_cwd": str(cwd),
                "session_id_observed": session_match.group(0)
                if session_match
                else None,
                "launch_intent_created": launch_intent_created,
                "startup_failure": str(error),
                "qualification": provider_failure_qualification(provider, output),
                "provider_output_observed": provider_native_output_observed(
                    provider, output
                ),
                "cleanup_scope": cleanup_scope,
                "provider_process_observed": cleanup_scope["provider_process_observed"],
                "launch_log": str(launch_log),
                "retry_intents_after_launch": read_retry_count(root),
            },
            live,
        )
    output = live.output.decode("utf-8", errors="replace")
    output = redact(output, token)
    (provider_root / "launch.log").write_text(output, encoding="utf-8")
    session_match = UUID_RE.search(output)
    launch_intent_created = any(
        retry_intent_matches_launch(
            intent,
            provider=provider,
            launcher_pid=live.process.pid,
            provider_cwd=cwd,
            session_id=session_match.group(0) if session_match else None,
        )
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
        "launcher_start_identity": process_start_identity(live.process.pid),
        "provider_config_root": str(provider_config_root),
        "provider_home": str(provider_home),
        "provider_cwd": str(cwd),
        "session_id_observed": session_match.group(0) if session_match else None,
        "launch_intent_created": launch_intent_created,
        "launch_log": str(provider_root / "launch.log"),
        "retry_intents_after_launch": read_retry_count(root),
    }
    owner = next(
        (
            intent
            for intent in read_retry_intents(root)
            if retry_intent_matches_launch(
                intent,
                provider=provider,
                launcher_pid=live.process.pid,
                provider_cwd=cwd,
                session_id=session_match.group(0) if session_match else None,
            )
        ),
        None,
    )
    if owner is not None:
        result["provider_pid"] = owner.get("provider_pid")
        result["provider_process_start_time"] = owner.get("provider_process_start_time")
    result["cleanup_scope"] = provider_cleanup_scope(
        profile_root=provider_config_root,
        provider_home=provider_home,
        cwd=cwd,
        launcher_pid=live.process.pid,
        launcher_start_identity=result.get("launcher_start_identity"),
        provider_pid=result.get("provider_pid"),
        provider_process_start_time=result.get("provider_process_start_time"),
        session_id=(
            str(owner.get("expected_session_id"))
            if owner and owner.get("expected_session_id")
            else session_match.group(0)
            if session_match
            else None
        ),
    )
    if result.get("provider_pid") is None:
        observed = observed_provider_record(result["cleanup_scope"], provider_bin)
        if observed is not None:
            result["provider_pid"] = int(observed["pid"])
            result["provider_process_start_time"] = process_start_identity(
                result["provider_pid"]
            )
            result["cleanup_scope"] = provider_cleanup_scope(
                profile_root=provider_config_root,
                provider_home=provider_home,
                cwd=cwd,
                launcher_pid=live.process.pid,
                launcher_start_identity=result.get("launcher_start_identity"),
                provider_pid=result["provider_pid"],
                provider_process_start_time=result.get("provider_process_start_time"),
                session_id=(
                    str(owner.get("expected_session_id"))
                    if owner and owner.get("expected_session_id")
                    else session_match.group(0)
                    if session_match
                    else None
                ),
            )
    result["provider_process_observed"] = result.get("provider_pid") is not None
    result["cleanup_scope"]["provider_process_observed"] = result[
        "provider_process_observed"
    ]
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
    if cleanup["status"] != "pass" and cleanup["status"] != "not_applicable":
        raise RuntimeError(
            f"{provider} detached cleanup required harness intervention: {cleanup}"
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
                profile_name=f"concurrent-{provider}",
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
        raise RuntimeError(f"Runtime Host launch-state database is missing: {database}")
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


def record_success_measurements(
    artifact: dict[str, Any],
    *,
    run_started_at: str,
    run_started_monotonic: float,
    host_outage_started_at: str | None,
    host_recovery_started_at: str | None,
    host_recovery_started_monotonic: float | None,
    recovery_completed_at: str | None,
    recovery_completed_monotonic: float | None,
    cleanup_completed_at: str,
    run_completed_at: str,
    run_completed_monotonic: float,
) -> dict[str, Any]:
    """Attach success timing only after the complete teardown has finished."""
    artifact["generated_at"] = run_completed_at
    artifact["measurements"] = {
        "run_started_at": run_started_at,
        "run_completed_at": run_completed_at,
        "run_duration_seconds": round(
            run_completed_monotonic - run_started_monotonic, 3
        ),
        "host_outage_started_at": host_outage_started_at,
        "host_recovery_started_at": host_recovery_started_at,
        "retry_queue_converged_at": recovery_completed_at,
        "recovery_duration_seconds": (
            round(
                recovery_completed_monotonic - host_recovery_started_monotonic,
                3,
            )
            if recovery_completed_monotonic is not None
            and host_recovery_started_monotonic is not None
            else None
        ),
        "cleanup_completed_at": cleanup_completed_at,
    }
    return artifact


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_started_at = utc_now()
    run_started_monotonic = time.monotonic()
    args._run_started_at = run_started_at
    args._run_started_monotonic = run_started_monotonic
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
    provider_auth: dict[str, Any] = {}
    host_outage_started_at: str | None = None
    host_recovery_started_at: str | None = None
    host_recovery_started_monotonic: float | None = None
    recovery_completed_at: str | None = None
    recovery_completed_monotonic: float | None = None
    cleanup_completed_at: str | None = None
    cleanup_completed_monotonic: float | None = None
    success_artifact: dict[str, Any] | None = None
    try:
        longhouse_bin = resolve_file(
            args.longhouse_bin or os.environ.get("LONGHOUSE_FAULT_LONGHOUSE_BIN"),
            "longhouse",
        )
        engine_bin = resolve_file(
            args.engine_bin or os.environ.get("LONGHOUSE_FAULT_ENGINE_BIN"),
            "longhouse-engine",
        )
        implementation_before = validated_implementation_snapshot(
            longhouse_bin, engine_bin
        )
        args._implementation_before = implementation_before
        provider_bins: dict[str, Path] = {}
        provider_evidence: dict[str, Any] = {}
        for provider in selected_providers:
            binary_name, env_name, _ = PROVIDER_BINARIES[provider]
            provider_bins[provider] = resolve_file(
                os.environ.get(env_name), binary_name
            )
            provider_evidence[provider] = version_probe(provider_bins[provider])
        if args.credentialed:
            for provider in selected_providers:
                provider_auth[provider] = bootstrap_provider_auth(
                    provider,
                    provider_bin=provider_bins[provider],
                    root=temp_root,
                )
        args._provider_auth = provider_auth

        port = choose_port()
        host = start_host(temp_root, evidence_root, port=port, ordinal=1)
        base_url = f"http://127.0.0.1:{port}"
        token = create_device_token(base_url)
        stop_host(host)
        stop_processes_for_root(temp_root)
        host = None
        host_outage_started_at = utc_now()

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
        cursor_auth = provider_auth.get("cursor")
        if cursor_auth and cursor_auth.get("status") == "missing":
            for result in results:
                if result.get("provider") == "cursor" and result.get("startup_failure"):
                    attributable_timeout = (
                        result.get("timed_out") is True
                        and result.get("degraded_marker_seen") is True
                        and result.get("launch_intent_created") is True
                        and result.get("provider_output_observed") is False
                    )
                    result["qualification"] = (
                        "harness_precondition_unmet"
                        if attributable_timeout
                        else "provider_owned_start_failure"
                    )
                    result["qualification_detail"] = (
                        cursor_auth.get("precondition_failure")
                        if attributable_timeout
                        else "cursor_startup_failure_not_attributable_to_missing_account_session"
                    )
                    if attributable_timeout:
                        result["qualification_basis"] = (
                            "cursor_status_probe_missing_and_no_provider_output"
                        )
        provider_failures = [
            result for result in results if result.get("startup_failure") is not None
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
            str(intent.get("provider_name", "")).lower() for intent in retry_intents
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
            raise RuntimeError(
                "installed retry intents contained no launcher ownership identity"
            )
        for result in results:
            matches = [
                intent
                for intent in retry_intents
                if retry_intent_matches_launch(
                    intent,
                    provider=str(result.get("provider", "")),
                    launcher_pid=result.get("launcher_pid"),
                    provider_cwd=Path(str(result.get("provider_cwd", ""))),
                    session_id=result.get("session_id_observed"),
                )
            ]
            if result.get("launch_intent_created") and len(matches) != 1:
                raise RuntimeError(
                    "installed launch did not have exactly one durable intent match: "
                    f"provider={result.get('provider')} "
                    f"launcher={result.get('launcher_pid')} matches={len(matches)}"
                )
            result["durable_intent_matched"] = len(matches) == 1
            result["provider_ready_durable"] = bool(
                matches and matches[0].get("provider_ready") is True
            )
        unready_intents = [
            intent
            for intent in retry_intents
            if intent.get("provider_ready") is not True
        ]
        if unready_intents and not args.allow_unqualified_recovery:
            providers = ", ".join(
                str(intent.get("provider_name") or "unknown")
                for intent in unready_intents
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
                cleanup = finish_live_command(
                    live,
                    provider_pid=result.get("provider_pid"),
                    provider_process_start_time=result.get(
                        "provider_process_start_time"
                    ),
                    cleanup_scope=result.get("cleanup_scope"),
                )
                if cleanup["status"] not in {"pass", "not_started"}:
                    raise RuntimeError(
                        f"{result['provider']} pre-ready provider cleanup failed: {cleanup}"
                    )
                result["returncode"] = cleanup["returncode"]
                result["detached_cleanup"] = cleanup
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
            cold_env["CODEX_HOME"] = str(temp_root / "provider-config" / "codex")

            def spawn_cold_agent(log_handle: Any) -> subprocess.Popen[bytes]:
                return subprocess.Popen(
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
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

            cold_engine = spawn_cold_agent(cold_handle)
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
                str(intent.get("expected_session_id"))
                for intent in current_retry_intents
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
            attempts_before_agent_restart = {
                str(intent.get("expected_session_id")): int(intent.get("attempts") or 0)
                for intent in read_retry_intents(temp_root)
                if str(intent.get("expected_session_id")) in ready_ids
            }
            kill_group(cold_engine)
            try:
                cold_engine.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_group(cold_engine, grace=0.1)
                try:
                    cold_engine.wait(timeout=2)
                except subprocess.TimeoutExpired as final_error:
                    cold_handle.close()
                    raise ProcessScanFailure(
                        "cold-start Machine Agent could not be reaped before restart"
                    ) from final_error
            cold_handle.close()
            engine = None
            engine_handle = None

            restart_handle = cold_log.open("ab")
            restarted_engine = spawn_cold_agent(restart_handle)
            time.sleep(2)
            second_start_returncode = restarted_engine.poll()
            if second_start_returncode is not None:
                restart_handle.close()
                raise RuntimeError(
                    "Machine Agent did not survive its cold restart during outage: "
                    f"{second_start_returncode}"
                )
            engine = restarted_engine
            engine_handle = restart_handle
            current_retry_intents = read_retry_intents(temp_root)
            current_retry_ids = {
                str(intent.get("expected_session_id"))
                for intent in current_retry_intents
            }
            if not ready_ids.issubset(current_retry_ids):
                raise RuntimeError(
                    "Machine Agent restart removed a provider-ready retry intent "
                    "during the outage"
                )
            preserved_retry_count = len(current_retry_intents)

            def cold_retry_progress_after_restart() -> bool:
                relevant_intents = [
                    intent
                    for intent in read_retry_intents(temp_root)
                    if str(intent.get("expected_session_id")) in ready_ids
                ]
                return bool(relevant_intents) and all(
                    int(intent.get("attempts") or 0)
                    >= attempts_before_agent_restart.get(
                        str(intent.get("expected_session_id")), 0
                    )
                    for intent in relevant_intents
                ) and any(
                    int(intent.get("attempts") or 0)
                    > attempts_before_agent_restart.get(
                        str(intent.get("expected_session_id")), 0
                    )
                    for intent in relevant_intents
                )

            max_attempts_before_restart = max(
                attempts_before_agent_restart.values(), default=0
            )
            retry_progress_timeout = max(
                15,
                2 ** min(max_attempts_before_restart, 8) * 2,
            )
            wait_for(
                cold_retry_progress_after_restart,
                retry_progress_timeout,
                "restarted Machine Agent to preserve durable retry progress while Runtime Host is unavailable",
            )
            retry_attempts_after_restart = {
                str(intent.get("expected_session_id")): int(intent.get("attempts") or 0)
                for intent in read_retry_intents(temp_root)
                if str(intent.get("expected_session_id")) in ready_ids
            }
            cold_restart_evidence = {
                "first_start_returncode": first_start_returncode,
                "first_start_survived_outage": True,
                "agent_restarted_during_outage": True,
                "second_start_returncode": second_start_returncode,
                "retry_attempts_before_agent_restart": attempts_before_agent_restart,
                "retry_attempts_after_agent_restart": retry_attempts_after_restart,
                "retry_attempts_observed": max(retry_attempts_after_restart.values()),
                "retry_intents_after_cold_start": preserved_retry_count,
                "pre_ready_intents_garbage_collected": None,
                "log": str(cold_log),
            }

        host_recovery_started_at = utc_now()
        host_recovery_started_monotonic = time.monotonic()
        host = start_host(temp_root, evidence_root, port=port, ordinal=2)
        if engine is None:
            engine_log = evidence_root / "machine-agent.log"
            engine_handle = engine_log.open("wb")
            engine_env = runtime_env(temp_root, engine_bin)
            engine_env["CODEX_HOME"] = str(temp_root / "provider-config" / "codex")
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
        recovery_completed_at = utc_now()
        recovery_completed_monotonic = time.monotonic()
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
            cleanup = finish_live_command(
                live,
                provider_pid=result.get("provider_pid"),
                provider_process_start_time=result.get("provider_process_start_time"),
                cleanup_scope=result.get("cleanup_scope"),
            )
            if cleanup["status"] != "pass":
                raise RuntimeError(
                    f"{result['provider']} provider cleanup failed after adoption: {cleanup}"
                )
            result["returncode"] = cleanup["returncode"]
            result["detached_cleanup"] = cleanup
        live_commands.clear()
        if engine_handle is not None:
            engine_handle.close()
            engine_handle = None
        implementation_after = validated_implementation_snapshot(
            longhouse_bin, engine_bin
        )
        assert_implementation_stable(implementation_before, implementation_after)
        success_artifact = {
            "schema_version": 1,
            "artifact_kind": "installed_managed_launch_fault_matrix",
            "generated_at": None,
            "verdict": "yellow" if unready_intents or provider_failures else "green",
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
                "longhouse": implementation_after["longhouse"],
                "longhouse_engine": implementation_after["longhouse_engine"],
            },
            "implementation_before": implementation_before,
            "harness": verified_harness_provenance(),
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
            "provider_auth": provider_auth,
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
        primary_exception = sys.exc_info()[1]
        cleanup_errors: list[str] = []
        for _, live in live_commands:
            try:
                result = next(
                    (
                        candidate
                        for candidate, candidate_live in live_commands
                        if candidate_live is live
                    ),
                    {},
                )
                cleanup = finish_live_command(
                    live,
                    provider_pid=result.get("provider_pid"),
                    provider_process_start_time=result.get(
                        "provider_process_start_time"
                    ),
                    cleanup_scope=result.get("cleanup_scope"),
                )
                if cleanup["status"] not in {"pass", "not_started"}:
                    cleanup_errors.append(
                        f"{result.get('provider', 'unknown')} cleanup status: {cleanup}"
                    )
            except Exception as error:  # noqa: BLE001 - cleanup must be reported.
                cleanup_errors.append(
                    f"live-command cleanup {type(error).__name__}: {error}"
                )
        live_commands.clear()
        if engine_handle is not None:
            engine_handle.close()
            engine_handle = None
        if engine is not None:
            kill_group(engine)
            try:
                engine.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                cleanup_errors.append(
                    f"Machine Agent cleanup did not finish: {error}"
                )
        try:
            stop_host(host)
        except Exception as error:  # noqa: BLE001 - cleanup must be reported.
            cleanup_errors.append(
                f"Runtime Host cleanup {type(error).__name__}: {error}"
            )
        try:
            stop_processes_for_root(temp_root)
        except Exception as error:  # noqa: BLE001 - cleanup must be reported.
            cleanup_errors.append(
                f"temporary-root cleanup {type(error).__name__}: {error}"
            )
        if os.environ.get("LONGHOUSE_KEEP_INSTALLED_FAULT_ROOT") != "1":
            try:
                remove_temporary_root(temp_root)
            except Exception as error:  # noqa: BLE001 - cleanup must be reported.
                cleanup_errors.append(
                    f"temporary-root deletion {type(error).__name__}: {error}"
                )
        cleanup_completed_at = utc_now()
        cleanup_completed_monotonic = time.monotonic()
        if cleanup_errors:
            details = "; ".join(cleanup_errors)
            if primary_exception is not None:
                raise ProcessScanFailure(
                    "primary matrix failure preserved: "
                    f"{type(primary_exception).__name__}: {primary_exception}; "
                    f"cleanup failures: {details}"
                ) from primary_exception
            raise ProcessScanFailure(f"installed matrix cleanup failed: {details}")

    if success_artifact is None:
        raise RuntimeError("installed matrix completed without a success artifact")
    if cleanup_completed_at is None or cleanup_completed_monotonic is None:
        raise RuntimeError("installed matrix completed without teardown timing")
    run_completed_at = utc_now()
    return record_success_measurements(
        success_artifact,
        run_started_at=run_started_at,
        run_started_monotonic=run_started_monotonic,
        host_outage_started_at=host_outage_started_at,
        host_recovery_started_at=host_recovery_started_at,
        host_recovery_started_monotonic=host_recovery_started_monotonic,
        recovery_completed_at=recovery_completed_at,
        recovery_completed_monotonic=recovery_completed_monotonic,
        cleanup_completed_at=cleanup_completed_at,
        run_completed_at=run_completed_at,
        run_completed_monotonic=time.monotonic(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--longhouse-bin", type=Path)
    parser.add_argument("--engine-bin", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--provider", action="append", choices=PROVIDERS)
    parser.add_argument(
        "--credentialed",
        action="store_true",
        help=(
            "Prepare provider-native auth in the disposable root using the "
            "credential environment; Codex uses login --with-api-key on stdin."
        ),
    )
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
            "provider_auth": getattr(args, "_provider_auth", {}),
            "harness": harness_provenance(),
            "measurements": {
                "run_started_at": getattr(args, "_run_started_at", None),
                "run_failed_at": utc_now(),
                "run_duration_seconds": (
                    round(
                        time.monotonic()
                        - float(getattr(args, "_run_started_monotonic")),
                        3,
                    )
                    if getattr(args, "_run_started_monotonic", None) is not None
                    else None
                ),
            },
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
    return (
        0
        if artifact["verdict"] == "green"
        or (artifact["verdict"] == "yellow" and args.allow_unqualified_recovery)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
