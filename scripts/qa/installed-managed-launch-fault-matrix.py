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
import hashlib
import json
import os
import pty
import re
import secrets
import select
import shutil
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
from typing import Any


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


def runtime_env(root: Path, engine_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "LONGHOUSE_HOME": str(root / "longhouse"),
            "LONGHOUSE_ENGINE_BIN": str(engine_bin),
            "PYTHONUNBUFFERED": "1",
            "TERM": env.get("TERM", "xterm-256color"),
        }
    )
    return env


def start_host(root: Path, evidence_root: Path, *, port: int, ordinal: int) -> Host:
    repo_root = Path(__file__).resolve().parents[2]
    server_root = repo_root / "server"
    venv_python = server_root / ".venv" / "bin" / "python"
    wait_for(lambda: port_is_available(port), 60, f"Runtime Host port {port} to become available")
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
    database = root / "runtime-host.db"
    log_path = evidence_root / f"runtime-host-{ordinal}.log"
    log = log_path.open("wb")
    env = os.environ.copy()
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


def create_device_token(base_url: str) -> str:
    code, body = http_json(
        f"{base_url}/api/devices/tokens",
        method="POST",
        payload={"name": "installed-managed-launch-fault-matrix", "device_id": DEVICE_ID},
    )
    token = body.get("token")
    if code not in {200, 201} or not isinstance(token, str) or not token.startswith("zdt_"):
        raise RuntimeError(f"Runtime Host did not issue a device token: status={code} body={body}")
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
        "stop_output": redact((stop.stdout or "") + (stop.stderr or ""), env.get("LONGHOUSE_DEVICE_TOKEN", "")),
        "remaining_process_groups": sorted(session_process_groups(session_id)),
    }


def wait_status(pid: int, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return 0
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
    timeout: float = 25,
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
            if marker_seen and not sent_interrupt and marker_at is not None and time.monotonic() - marker_at >= 3:
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


def provider_command(
    provider: str,
    *,
    longhouse_bin: Path,
    provider_bin: Path,
    root: Path,
    cwd: Path,
    base_url: str,
    token: str,
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
        command.extend(["--no-attach", "--claude-dir", str(root / "provider-config" / "opencode")])
    elif provider == "codex":
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
        "command": ["<device-token-redacted>" if value == token else value for value in command],
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


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = (args.evidence_root or repo_root / ".build/canaries/installed-managed-launch-fault-matrix" / stamp).resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    args._resolved_evidence_root = evidence_root
    # Keep this root short: Codex's bridge IPC socket is subject to macOS
    # SUN_LEN, and the qualification must exercise the installed path rather
    # than fail because the test harness chose a long temporary directory.
    temp_root = Path(tempfile.mkdtemp(prefix="lh.", dir="/tmp"))
    args._temporary_root = temp_root
    host: Host | None = None
    engine: subprocess.Popen[bytes] | None = None
    results: list[dict[str, Any]] = []
    args._matrix_results = results
    selected_providers = tuple(args.provider or PROVIDERS)
    args._selected_providers = selected_providers
    try:
        longhouse_bin = resolve_file(args.longhouse_bin or os.environ.get("LONGHOUSE_FAULT_LONGHOUSE_BIN"), "longhouse")
        engine_bin = resolve_file(args.engine_bin or os.environ.get("LONGHOUSE_FAULT_ENGINE_BIN"), "longhouse-engine")
        provider_bins: dict[str, Path] = {}
        provider_evidence: dict[str, Any] = {}
        for provider in selected_providers:
            binary_name, env_name, _ = PROVIDER_BINARIES[provider]
            provider_bins[provider] = resolve_file(os.environ.get(env_name), binary_name)
            provider_evidence[provider] = version_probe(provider_bins[provider])

        port = choose_port()
        host = start_host(temp_root, evidence_root, port=port, ordinal=1)
        base_url = f"http://127.0.0.1:{port}"
        token = create_device_token(base_url)
        stop_host(host)
        host = None

        for provider in selected_providers:
            result = run_provider(
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
        retry_count = read_retry_count(temp_root)
        if retry_count != len(selected_providers):
            raise RuntimeError(
                f"installed outage launches left {retry_count} retry intents, expected {len(selected_providers)}"
            )

        host = start_host(temp_root, evidence_root, port=port, ordinal=2)
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

        wait_for(
            lambda: read_retry_count(temp_root) == 0,
            60,
            "installed registration retry convergence after Runtime Host recovery",
        )
        engine_handle.close()
        return {
            "schema_version": 1,
            "artifact_kind": "installed_managed_launch_fault_matrix",
            "generated_at": utc_now(),
            "verdict": "green",
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
                "machine_agent_registration_recovery_after_runtime_host_restart",
            ],
            "providers": results,
            "retry_intents_before_recovery": len(selected_providers),
            "retry_intents_after_recovery": 0,
            "runtime_host_port": port,
            "machine_agent_log": str(engine_log),
            "evidence_root": str(evidence_root),
        }
    finally:
        if engine is not None:
            kill_group(engine)
            try:
                engine.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        stop_host(host)
        if os.environ.get("LONGHOUSE_KEEP_INSTALLED_FAULT_ROOT") != "1":
            shutil.rmtree(temp_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--longhouse-bin", type=Path)
    parser.add_argument("--engine-bin", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--provider", action="append", choices=PROVIDERS)
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
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"verdict: {artifact['verdict']}")
        print(f"artifact: {artifact_path}")
        if artifact["verdict"] != "green":
            print(artifact.get("error", "installed fault matrix failed"), file=sys.stderr)
    return 0 if artifact["verdict"] == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
