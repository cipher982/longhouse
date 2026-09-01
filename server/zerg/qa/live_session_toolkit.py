"""Shared live-session toolkit for provider qualification producers.

Everything here was reached into as `zerg.qa.provider_native_resume`'s privates
by seventeen sibling producers, which is what a missing library layer looks
like: an isolated provider home, a PTY session, a transcript shipper, the waits
that decide when a real turn has finished, and the secret-scan/redaction rules
that keep evidence publishable. None of it is specific to Resume; the resume
producer is one of its callers.

Names siblings depend on are public. The private ones are internal mechanics of
this module.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http
import json
import os
import pty
import pwd
import re
import select
import signal
import socket
import sqlite3
import subprocess
import termios
import time
import urllib.error
import urllib.request
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file

QUALIFICATION_SANDBOX_ENV = "LONGHOUSE_QUALIFICATION_SANDBOX"


QUALIFICATION_HOME_ENV = "LONGHOUSE_QUALIFICATION_HOME"


QUALIFICATION_SANDBOX_PROFILE = "provider-qualification-bwrap-v3"


_ANSI_CONTROL_RE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_]|.)")


_RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"


RUNTIME_API_URL_ENV = "LONGHOUSE_RUNTIME_API_URL"


RUNTIME_AGENTS_TOKEN_ENV = "LONGHOUSE_RUNTIME_AGENTS_TOKEN"


_EVIDENCE_SECRET_KEY_RE = re.compile(r"(?:^|_)(?:token|secret|password|api_key|access_key|authorization)$")


_TRANSCRIPT_SHIP_MAX_ATTEMPTS = 3


_STORAGE_LANE_BUSY_MAX_SLEEP_SECS = 10.0


_STORAGE_LANE_BUSY_RE = re.compile(r"storage-v2 repair lane busy; retry after (?P<milliseconds>\d+)ms")


_HTTP_STATUS_ERROR_RE = re.compile(r"HTTP status [^\r\n]*\((?P<status>[45]\d{2}) [^)]+\)")


_TRANSCRIPT_CAPABILITY_RETRY_SLEEP_SECS = 1.0


_SAFE_DIAGNOSTIC_DETAIL_RE = re.compile(r"^[a-z0-9][a-z0-9_.:+-]{0,127}$")


_CURSOR_DIAGNOSTIC_PAYLOAD_KEYS = frozenset({"generation_id", "status", "phase", "is_interrupt", "text"})


_MAX_RETAINED_TERMINAL_BYTES = 64 * 1024


_TERMINAL_RECORDINGS = ("initial.tty", "native-resume.tty", "concurrent-resume-attempt.tty", "claude-onboarding.tty")


def qualification_secrets(environment: dict[str, str], agents_token: str) -> tuple[str, ...]:
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

    def _database_diagnostics(self) -> dict[str, Any]:
        """Retain enough metadata to locate a SQLite ownership failure.

        The qualification sandbox is destroyed after every attempt. A bad DB
        header used to leave only SQLite error 26, making it impossible to tell
        whether the daemon created invalid state or the one-shot flush changed
        a valid database. This records no rows or transcript content.
        """

        diagnostic: dict[str, Any] = {
            "path_name": self.db_path.name,
            "exists": self.db_path.is_file(),
        }
        try:
            with self.db_path.open("rb") as stream:
                header = stream.read(16)
            diagnostic.update(
                {
                    "size_bytes": self.db_path.stat().st_size,
                    "header_hex": header.hex(),
                    "header_sha256": hashlib.sha256(header).hexdigest(),
                    "sqlite_header": header == b"SQLite format 3\x00",
                    "wal_size_bytes": self.db_path.with_name(f"{self.db_path.name}-wal").stat().st_size
                    if self.db_path.with_name(f"{self.db_path.name}-wal").is_file()
                    else 0,
                    "shm_size_bytes": self.db_path.with_name(f"{self.db_path.name}-shm").stat().st_size
                    if self.db_path.with_name(f"{self.db_path.name}-shm").is_file()
                    else 0,
                }
            )
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
                diagnostic["quick_check"] = row[0] if row else None
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        return diagnostic

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
        database_before_ship = self._database_diagnostics()
        attempts: list[dict[str, Any]] = []
        log_sections: list[str] = []
        retry_reasons_seen: set[str] = set()
        # A failed admission quarantines the envelope and the next ship
        # invocation can reconcile a missing Cursor predecessor. A typed
        # storage-lane backpressure response is also safe to retry because the
        # same durable request body remains authoritative. Retry only those
        # explicit transient states, never arbitrary engine failures.
        for attempt in range(1, _TRANSCRIPT_SHIP_MAX_ATTEMPTS + 1):
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
                attempts.append(result)
                break
            except OSError as exc:
                result = {
                    "status": "fail",
                    "label": label,
                    "exit_code": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                attempts.append(result)
                break
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
            attempt_retry_reason: str | None = None
            retry_delay_secs: float | None = None
            if completed.returncode != 0:
                busy_match = _STORAGE_LANE_BUSY_RE.search(stderr)
                if busy_match is not None:
                    advertised_delay_ms = int(busy_match.group("milliseconds"))
                    attempt_retry_reason = "storage_lane_busy"
                    retry_delay_secs = min(advertised_delay_ms / 1000, _STORAGE_LANE_BUSY_MAX_SLEEP_SECS)
                    result["retry_after_ms"] = advertised_delay_ms
                    result["retry_sleep_secs"] = retry_delay_secs
                elif "source_epoch_conflict_unresolved" in stderr:
                    attempt_retry_reason = "source_epoch_conflict_unresolved"
                elif all(
                    marker in stderr
                    for marker in (
                        "storage-v2 envelope POST returned 503",
                        '"code":"resource_exhausted"',
                        "catalog read lane is full",
                    )
                ):
                    result["failure_code"] = "storage_v2_catalog_read_lane_full"
                    result["http_status"] = 503
                    result["http_status_phrase"] = http.HTTPStatus.SERVICE_UNAVAILABLE.phrase
                    attempt_retry_reason = "storage_v2_catalog_read_lane_full"
                    retry_delay_secs = _TRANSCRIPT_CAPABILITY_RETRY_SLEEP_SECS
                    result["retry_sleep_secs"] = retry_delay_secs
                elif "storage-v2 capability request returned non-2xx" in stderr:
                    result["failure_code"] = "storage_v2_capability_request_failed"
                    status_match = _HTTP_STATUS_ERROR_RE.search(stderr)
                    status = int(status_match.group("status")) if status_match is not None else None
                    if status is not None:
                        result["http_status"] = status
                        try:
                            result["http_status_phrase"] = http.HTTPStatus(status).phrase
                        except ValueError:
                            pass
                    if status in {429, 502, 503, 504}:
                        attempt_retry_reason = "storage_v2_capability_unavailable"
                        retry_delay_secs = _TRANSCRIPT_CAPABILITY_RETRY_SLEEP_SECS
                        result["retry_sleep_secs"] = retry_delay_secs
                elif "storage-v2 capability request failed" in stderr and "operation timed out" in stderr:
                    result["failure_code"] = "storage_v2_capability_request_failed"
                    result["transport_error"] = "operation_timed_out"
                    attempt_retry_reason = "storage_v2_capability_unavailable"
                    retry_delay_secs = _TRANSCRIPT_CAPABILITY_RETRY_SLEEP_SECS
                    result["retry_sleep_secs"] = retry_delay_secs
            if attempt_retry_reason is not None:
                result["retry_reason"] = attempt_retry_reason
            attempts.append(result)
            log_sections.append(f"attempt {attempt}\nstdout:\n{stdout}\nstderr:\n{stderr}\n")
            if completed.returncode == 0:
                break
            if attempt_retry_reason is None or attempt_retry_reason in retry_reasons_seen or attempt >= _TRANSCRIPT_SHIP_MAX_ATTEMPTS:
                break
            retry_reasons_seen.add(attempt_retry_reason)
            if retry_delay_secs is None:
                continue
            time.sleep(retry_delay_secs)
        result = dict(attempts[-1])
        result["database_before_ship"] = database_before_ship
        result["database_after_ship"] = self._database_diagnostics()
        result["attempts"] = len(attempts)
        retry_reasons = [attempt["retry_reason"] for attempt in attempts if "retry_reason" in attempt]
        if retry_reasons:
            result["retry_reasons"] = retry_reasons
            result["retry_reason"] = retry_reasons[0] if len(set(retry_reasons)) == 1 else "mixed"
        retry_after_values = [attempt["retry_after_ms"] for attempt in attempts if "retry_after_ms" in attempt]
        if retry_after_values:
            result["retry_after_ms"] = retry_after_values[0]
            result["retry_after_ms_by_attempt"] = retry_after_values
        retry_sleep_values = [attempt["retry_sleep_secs"] for attempt in attempts if "retry_sleep_secs" in attempt]
        if retry_sleep_values:
            result["retry_sleep_secs"] = retry_sleep_values[0]
            result["retry_sleep_secs_by_attempt"] = retry_sleep_values
        log_path = self.evidence_root / f"transcript-flush-{label}.log"
        log_path.write_text("\n".join(log_sections), encoding="utf-8")
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
            result["database_after_restart_failure"] = self._database_diagnostics()
        return result

    def capture_cursor_projection_diagnostics(
        self,
        state: dict[str, Any],
        *,
        marker: str,
        label: str,
    ) -> str:
        """Retain source and engine state at the strict projection boundary.

        Cursor exposes provider activity through hooks before its transcript
        projections are necessarily visible to the engine.  This snapshot is
        diagnostic only: it never participates in acceptance.  It is kept
        deliberately metadata-only so a failed canary can distinguish provider
        persistence, source discovery, and engine capture without copying raw
        transcript content into the proof bundle.
        """

        path = self.evidence_root / f"cursor-projection-diagnostics-{label}.json"
        try:
            payload = _cursor_projection_diagnostics(
                environment=self.engine_environment,
                state=state,
                marker=marker,
                engine_db_path=self.db_path,
                phase=label,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics never own the verdict
            payload = {
                "schema": "cursor_projection_diagnostics_unavailable.v1",
                "phase": label,
                "captured_at": now(),
                "provider": "cursor",
                "session_id": state.get("session_id"),
                "provider_session_id": state.get("provider_session_id"),
                "marker": marker,
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }
        _write_best_effort_json(path, payload)
        return str(path)

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
                "process_group_dead": wait_process_group_dead(self.process.pid),
            }
        )
        try:
            self.log_stream.close()
        except OSError:
            pass
        return self.receipt


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
        self._closed = False
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
        if self._closed:
            return
        try:
            self.drain()
        finally:
            self._stream.close()
            self._closed = True
        try:
            os.close(self.master)
        except OSError:
            pass


class RuntimeHostRegistrationTransient(RuntimeError):
    """A managed resume could not establish fresh Runtime Host ownership."""


def _raise_known_registration_transient(process: PtyProcess) -> None:
    """Turn only the CLI's fixed registration diagnostics into a typed retry."""

    terminal = _terminal_text(process.recording)
    timed_out = "register managed Claude resume launch" in terminal and ("timed out" in terminal or "operation timed out" in terminal)
    unavailable = "managed Claude resume launch failed: Runtime Host returned HTTP" in terminal and any(
        f"HTTP {status}" in terminal for status in (408, 502, 503, 504)
    )
    if timed_out or unavailable:
        raise RuntimeHostRegistrationTransient("managed Claude resume registration temporarily unavailable")


def write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_best_effort_json(path: Path, payload: object) -> bool:
    """Write diagnostic evidence without taking ownership of the verdict."""

    try:
        write_json(path, payload)
    except Exception:  # noqa: BLE001 - diagnostic persistence is non-authoritative
        return False
    return True


def _process_group_dead(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def wait_process_group_dead(pid: int, timeout: float = 5) -> bool:
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


def wait_pid_dead(pid: int, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_dead(pid):
            return True
        time.sleep(0.1)
    return _pid_dead(pid)


def _provision_transcript_roots(home: Path, environment: dict[str, str]) -> None:
    """Create the provider roots the real engine discovers at startup."""

    xdg_config_home = Path(str(environment.get("XDG_CONFIG_HOME") or home / ".config"))
    roots = [
        home / ".codex" / "sessions",
        home / ".local" / "share" / "opencode",
        home / ".cursor" / "chats",
        xdg_config_home / "cursor" / "chats",
        # Cursor's durable project store is the authoritative source for
        # resumed conversations.  Create the discovery root before the
        # Machine Agent starts so the initial launch and the resumed launch
        # use one enrolled storage-v2 source instead of switching from the
        # JSONL mirror to a newly discovered store on restart.
        home / ".cursor" / "projects",
        home / ".longhouse" / "agent" / "cursor-acp-source",
    ]
    configured_claude_dir = str(environment.get("CLAUDE_CONFIG_DIR") or "").strip()
    roots.append(Path(configured_claude_dir) / "projects" if configured_claude_dir else home / ".claude" / "projects")
    for path in roots:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def start_transcript_shipper(
    provider: str,
    args: argparse.Namespace,
    *,
    home: Path,
    environment: dict[str, str],
    evidence_root: Path,
    longhouse_home: Path | None = None,
) -> TranscriptShipper:
    """Start the same file-watching Machine Agent used outside the factory."""

    # This helper owns its nested evidence tree. Callers should not need to
    # duplicate its internal log layout merely to make the first open succeed.
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    write_json(
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
                redaction_secrets=(*qualification_secrets(environment, args.agents_token),),
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


def provider_process_pid(spec: ProviderSpec, state: dict[str, Any]) -> int:
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


def cleanup_processes(
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
            provider_pids.append(provider_process_pid(spec, state))
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
            "process_group_dead": wait_process_group_dead(process.pid),
        }
        for process in processes
        if process is not None
    ]
    provider_process_receipts = [{"pid": pid, "process_dead": wait_pid_dead(pid)} for pid in provider_pids]
    attach_process_receipts = [{"pid": pid, "process_dead": wait_pid_dead(pid)} for pid in attach_pids]
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


def bound_terminal_recordings(
    root: Path,
    *,
    provider: str,
    states: list[dict[str, Any]],
    recording_names: Collection[str] | None = None,
    checkpoint_name: str | None = "native-resume-terminal-checkpoint.json",
) -> None:
    """Retain diagnostic PTY head/tail while keeping proof bytes bounded.

    Runtime Host transcripts and bridge receipts are the Resume authority; raw
    full-screen redraws are diagnostics. Keeping an unbounded TTY copy inside
    every v3 proof duplicates terminal noise and can crowd out the actual
    evidence. The receipt binds both the observed source and retained slice.
    """

    rows: list[dict[str, Any]] = []
    for name in recording_names or _TERMINAL_RECORDINGS:
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        original_size = path.stat().st_size
        original_digest = sha256_file(path)
        truncated = original_size > _MAX_RETAINED_TERMINAL_BYTES
        if truncated:
            half = _MAX_RETAINED_TERMINAL_BYTES // 2
            with path.open("rb") as stream:
                head = stream.read(half)
                stream.seek(-half, os.SEEK_END)
                tail = stream.read(half)
            omitted = original_size - len(head) - len(tail)
            marker = f"\n[longhouse: {omitted} terminal bytes omitted]\n".encode()
            retained = head + marker + tail
            temporary = path.with_name(f".{path.name}.{os.getpid()}.bounded")
            temporary.write_bytes(retained)
            temporary.replace(path)
        rows.append(
            {
                "path": name,
                "truncated": truncated,
                "original_size": original_size,
                "original_sha256": original_digest,
                "retained_size": path.stat().st_size,
                "retained_sha256": sha256_file(path),
            }
        )
    if rows and checkpoint_name is not None:
        initial_state = states[0] if states else {}
        resumed_state = states[-1] if len(states) > 1 else {}
        write_json(
            root / checkpoint_name,
            {
                "policy": "head_tail_v1",
                "maximum_source_bytes_retained": _MAX_RETAINED_TERMINAL_BYTES,
                "provider": provider,
                "session_id": resumed_state.get("session_id"),
                "initial_run_id": initial_state.get("run_id"),
                "resumed_run_id": resumed_state.get("run_id"),
                "same_session": bool(initial_state) and initial_state.get("session_id") == resumed_state.get("session_id"),
                "new_run": bool(initial_state) and initial_state.get("run_id") != resumed_state.get("run_id"),
                "native_resume_ready": any(row["path"] == "native-resume.tty" for row in rows),
                "recordings": rows,
            },
        )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _read_json_bounded(path: Path, *, max_bytes: int = 65536) -> dict[str, Any]:
    """Read provider-owned state without allowing an oversized diagnostic input."""

    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"provider JSON exceeds {max_bytes} bytes")
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _terminal_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return _ANSI_CONTROL_RE.sub("", raw.decode("utf-8", "replace"))


def latest_claude_startup_prompt(compact_terminal: str) -> str | None:
    """Return the newest Claude choice prompt in an append-only recording."""

    positions = {
        "permission": compact_terminal.rfind("Yes,Iaccept"),
        "trust": compact_terminal.rfind("Yes,Itrustthisfolder"),
        "channel": compact_terminal.rfind("Iamusingthisforlocaldevelopment"),
        "theme": compact_terminal.rfind("Choosethetextstyle"),
        "api_key": compact_terminal.rfind("DetectedacustomAPIkey"),
        "security_notes": max(compact_terminal.rfind("Securitynotes"), compact_terminal.rfind("PressEnte")),
    }
    prompt, position = max(positions.items(), key=lambda item: item[1])
    return prompt if position >= 0 else None


def _accept_claude_permission_prompt(process: PtyProcess) -> None:
    """Accept Claude's first-run bypass-permissions acknowledgement.

    The managed facade deliberately launches Claude in the sandboxed bypass
    mode used by the qualification.  Recent Claude builds add a native TUI
    acknowledgement for that mode before they publish the channel state.  It
    is a provider startup control record, not a user turn, so the live probe
    must acknowledge it before looking for the registered session.
    """

    if getattr(process, "claude_permission_acceptance_sent", False):
        return
    compact = re.sub(r"\s+", "", _terminal_text(process.recording))
    if "1.No,exit" in compact and "2.Yes,Iaccept" in compact:
        # The provider renders numbered choices and accepts the visible
        # choice key directly. This avoids depending on whether its selector
        # currently installed normal- or application-cursor mode.
        process.send("2\r")
        process.claude_permission_acceptance_sent = True
        return
    if "No,exit" not in compact or "Yes,Iaccept" not in compact:
        return

    # Claude 2.1.252 also renders this as an unnumbered, safety-first
    # selector. Give the initial screen and the selection repaint their own
    # boundaries; either key can be lost when it shares a render boundary.
    now = time.monotonic()
    seen_at = getattr(process, "claude_permission_prompt_seen_at", None)
    if seen_at is None:
        process.claude_permission_prompt_seen_at = now
        return
    selected_at = getattr(process, "claude_permission_selection_sent_at", None)
    if selected_at is None:
        if now - seen_at < 1.0:
            return
        process.send("\x1b[B")
        process.claude_permission_selection_sent_at = now
        return
    if now - selected_at < 1.0:
        return
    process.send("\r")
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


def wait_cursor_tui_ready(process: PtyProcess, recording: Path, *, timeout: float = 30.0) -> None:
    """Give Cursor time to render and accept its native workspace gate.

    Cursor writes its managed lease before the interactive trust screen is
    necessarily rendered. Waiting only for the lease, or doing one immediate
    prompt check after a short PTY settle, can send the first qualification
    message into a still-blocked TUI. Keep draining the real PTY until the
    prompt is handled or the bounded startup window expires.
    """

    del recording  # The process owns the append-only PTY recording.
    deadline = time.monotonic() + timeout
    ready_since: float | None = None
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("cursor Helm process exited before its TUI became ready")
        _accept_cursor_workspace_trust(process)
        if _cursor_tui_input_ready(_terminal_text(process.recording)):
            now = time.monotonic()
            if ready_since is None:
                ready_since = now
            elif now - ready_since >= 1.0:
                # A resumed Cursor TUI can briefly paint the previous prompt
                # before it finishes restoring the conversation. Require the
                # latest visible prompt to remain valid for a bounded quiet
                # interval before injecting anything into the PTY.
                process.settle(minimum=0.1, quiet=0.15, timeout=1.0)
                if _cursor_tui_input_ready(_terminal_text(process.recording)):
                    return
                ready_since = None
        else:
            ready_since = None
        time.sleep(0.1)

    raise RuntimeError(
        f"Cursor TUI did not publish a stable post-restore input prompt (tail={_terminal_text(process.recording)[-1200:]!r})"
    )


def _cursor_tui_input_ready(terminal: str) -> bool:
    """Recognize Cursor's post-trust prompt before injecting the first turn.

    The native launcher writes its lease before Cursor has finished switching
    from the workspace-trust screen to the prompt bar. Sending during that
    transition leaves the text rendered in the bar but never submits a
    foreground conversation, so no ``store.db`` exists for transcript proof.
    """

    normalized = re.sub(r"\s+", " ", _ANSI_CONTROL_RE.sub(" ", terminal)).lower()
    compact = re.sub(r"[^a-z0-9?]+", "", normalized)
    ready_positions = [compact.rfind(marker) for marker in ("plansearchbuildanything", "addafollowup", "promptbar")]
    ready_position = max(ready_positions)
    if ready_position < 0:
        return False
    # The recording is append-only and includes the restored conversation.
    # Compare the latest visible state markers instead of rejecting any old
    # loading text or accepting the first prompt from the restored transcript.
    blocked_positions = [
        compact.rfind(marker)
        for marker in (
            "workspacetrustrequired",
            "trustingworkspace",
            "loadingconversation",
            "restoringconversation",
            "startingconversation",
            "working",
        )
    ]
    if max(blocked_positions) > ready_position:
        return False
    return True


def _state_candidate_diagnostics(spec: ProviderSpec, home: Path) -> list[dict[str, Any]]:
    """Retain provider-state identity without copying private state fields."""

    rows: list[dict[str, Any]] = []
    for path in state_candidates(spec, home):
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


def wait_opencode_tui_ready(process: PtyProcess, recording: Path, *, timeout: float = 120.0) -> None:
    """Wait for the attached TUI, not merely the localhost bridge, to accept input."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.drain()
        if process.process.poll() is not None:
            raise RuntimeError("opencode Helm process exited before its TUI became ready")
        if _opencode_tui_is_connected(_terminal_text(recording)):
            return
        time.sleep(0.1)
    raise RuntimeError("opencode TUI did not publish its connected state")


def _opencode_tui_is_connected(terminal: str) -> bool:
    normalized = re.sub(r"\s+", " ", _ANSI_CONTROL_RE.sub(" ", terminal)).lower()
    # The native bridge logs "event monitor disconnected" into the same PTY.
    # A substring check therefore declared the TUI ready while it was actually
    # reporting a failed SSE connection.
    if re.search(r"\b(?:longhouse|opencode)\s+connected(?=\s|lsp|$)", normalized) is not None:
        return True
    return re.search(r"\bopencode\b", normalized) is not None and re.search(r"(?<!dis)\bconnected\b", normalized) is not None


def prepare_claude_profile(
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
    trust_prompt_seen_at: float | None = None
    trust_selection_sent_at: float | None = None
    started = time.monotonic()
    try:
        deadline = started + timeout
        while time.monotonic() < deadline:
            process.drain()
            if process.process.poll() is not None:
                raise RuntimeError("Claude onboarding process exited before profile completion")
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
            startup_prompt = latest_claude_startup_prompt(compact)
            if startup_prompt == "permission":
                _accept_claude_permission_prompt(process)
            elif startup_prompt == "theme" and theme_attempts == 0:
                process.send("\r")
                theme_attempts += 1
            elif startup_prompt == "api_key" and api_key_attempts == 0:
                process.send("\x1b[A")
                api_key_attempts = 1
            elif startup_prompt == "api_key" and api_key_attempts == 1:
                process.send("\r")
                api_key_attempts = 2
            elif startup_prompt == "security_notes" and security_notes_attempts == 0:
                process.send("\r")
                security_notes_attempts += 1
            elif not confirmed_trust and startup_prompt == "trust":
                # Claude's safety-first selector defaults to ``No, exit``.
                # Give both the initial selector and its changed selection a
                # full repaint boundary before sending the next key.
                now = time.monotonic()
                if trust_prompt_seen_at is None:
                    trust_prompt_seen_at = now
                elif trust_selection_sent_at is None and now - trust_prompt_seen_at >= 1.0:
                    process.send("\x1b[B")
                    trust_selection_sent_at = now
                elif trust_selection_sent_at is not None and now - trust_selection_sent_at >= 1.0:
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


def state_candidates(spec: ProviderSpec, home: Path) -> list[Path]:
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


def redact_state_for_evidence(value: Any) -> Any:
    """Keep provider identity state useful without retaining bridge secrets."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if _EVIDENCE_SECRET_KEY_RE.search(normalized) or normalized.endswith(("_token", "_secret", "_password", "_api_key")):
                redacted[str(key)] = "<redacted>"
            else:
                redacted[str(key)] = redact_state_for_evidence(item)
        return redacted
    if isinstance(value, list):
        return [redact_state_for_evidence(item) for item in value]
    return value


def wait_state(
    spec: ProviderSpec,
    home: Path,
    *,
    session_id: str | None = None,
    prior_run_id: str | None = None,
    timeout: float = 45,
    process: PtyProcess | None = None,
    exclude_paths: Collection[Path] | None = None,
) -> dict[str, Any]:
    excluded = {path.resolve() for path in (exclude_paths or ())}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None:
            process.drain()
            if process.process.poll() is not None:
                _raise_known_registration_transient(process)
                diagnostics = _state_candidate_diagnostics(spec, home)
                raise RuntimeError(
                    f"{spec.provider} Helm process exited before publishing state; candidates={json.dumps(diagnostics, sort_keys=True)}"
                )
            if spec.provider == "claude":
                _accept_claude_permission_prompt(process)
                _accept_claude_development_channel_prompt(process)
            elif spec.provider == "cursor":
                _accept_cursor_workspace_trust(process)
        for path in state_candidates(spec, home):
            if path.resolve() in excluded:
                continue
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


def wait_session_tail(
    api_url: str,
    token: str,
    session_id: str,
    *,
    timeout: float = 45,
    allow_unprojected: bool = False,
) -> dict[str, Any]:
    """Wait until the Runtime Host has projected a newly registered session.

    The managed launch transaction and transcript/runtime projection are
    separate writes.  A native provider can be ready before the catalog has
    created its first served session row, so an immediate tail request may
    legitimately return 404.  Treat application-level projection errors as
    transient here; authentication errors remain fatal.
    """

    deadline = time.monotonic() + timeout
    last_error = "session transcript was not projected"
    last_status: int | None = None
    while time.monotonic() < deadline:
        try:
            return _api_json(api_url, token, f"sessions/{session_id}/tail?limit=100&roles=user,assistant")
        except _RuntimeHostHTTPError as exc:
            if exc.status in {401, 403}:
                raise
            last_status = exc.status
            last_error = str(exc)
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    if allow_unprojected and last_status == 404:
        # Claude's native channel has no transcript event until the first
        # control message is delivered. Its channel bridge is already the
        # authoritative local owner, so an empty tail is the correct baseline
        # for that bootstrap message; the post-send assertion still requires a
        # real projected assistant response.
        return {}
    raise RuntimeError(f"Runtime Host did not project session {session_id} before the initial control send: {last_error}")


def _assistant_contains(value: Any, marker: str) -> bool:
    if isinstance(value, dict):
        role = str(value.get("role") or value.get("type") or "").lower()
        if role == "assistant" and marker in json.dumps(value, ensure_ascii=False):
            return True
        return any(_assistant_contains(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_assistant_contains(item, marker) for item in value)
    return False


def assistant_event_digests(value: Any) -> set[str]:
    """Identify top-level assistant events without retaining their content."""

    if isinstance(value, dict):
        role = str(value.get("role") or value.get("type") or "").lower()
        if role == "assistant":
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
            return {hashlib.sha256(encoded).hexdigest()}
        found: set[str] = set()
        for item in value.values():
            found.update(assistant_event_digests(item))
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found.update(assistant_event_digests(item))
        return found
    return set()


def wait_assistant_response_after_marker(
    api_url: str,
    token: str,
    session_id: str,
    marker: str,
    *,
    prior_assistant_event_digests: set[str],
    require_assistant_marker: bool = False,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prove a native control record caused a new provider response.

    Claude transcripts may store the submitted marker as a user event, and its
    model safety behavior may refuse to repeat a token received from that
    channel. Claude therefore correlates the native marker with a *new*
    assistant event. Providers whose response contract echoes the marker must
    satisfy the stricter assistant-content correlation so an unrelated new
    response cannot pass the probe.
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
        marker_observed_in_assistant = _assistant_contains(last, marker)
        observed_assistant_event_digests = assistant_event_digests(last)
        new_assistant_events = observed_assistant_event_digests - prior_assistant_event_digests
        if marker_observed and new_assistant_events and (not require_assistant_marker or marker_observed_in_assistant):
            return last, {
                "method": (
                    "assistant_marker_then_new_assistant_event"
                    if require_assistant_marker
                    else "transcript_marker_then_new_assistant_event"
                ),
                "marker_observed_in_transcript": marker_observed,
                "marker_observed_in_assistant": marker_observed_in_assistant,
                "prior_assistant_events": len(prior_assistant_event_digests),
                "observed_assistant_events": len(observed_assistant_event_digests),
                "new_assistant_events": len(new_assistant_events),
                "timed_out": False,
            }
        time.sleep(0.5)
    return last, {
        "method": (
            "assistant_marker_then_new_assistant_event" if require_assistant_marker else "transcript_marker_then_new_assistant_event"
        ),
        "marker_observed_in_transcript": marker_observed,
        "marker_observed_in_assistant": _assistant_contains(last, marker),
        "prior_assistant_events": len(prior_assistant_event_digests),
        "observed_assistant_events": len(observed_assistant_event_digests),
        "new_assistant_events": len(observed_assistant_event_digests - prior_assistant_event_digests),
        "timed_out": True,
    }


def _cursor_interrupt_to_idle(
    state: dict[str, Any],
    environment: dict[str, str],
    process: PtyProcess,
    *,
    timeout: float = 30.0,
    diagnostic_path: Path | None = None,
) -> dict[str, Any]:
    """Cancel a stranded Cursor generation and prove the owner is idle again.

    Cursor can persist an assistant response before its native ``stop`` hook
    runs.  That leaves the TUI painting ``Thinking`` and the Helm socket
    correctly rejects the next control request.  The response correlation
    remains the semantic completion oracle; Ctrl-C is a bounded recovery for
    the provider UI, not a substitute for that oracle.  Every recovery fact is
    tied to the enrolled session, conversation, launch, and active generation.
    """

    longhouse_home = str(environment.get("LONGHOUSE_HOME") or "").strip()
    if not longhouse_home:
        raise RuntimeError("Cursor clean stop requires an explicit Longhouse home")
    root = Path(longhouse_home) / "managed-local" / "cursor-helm"
    events_path = root / "hook-events" / f"{state['session_id']}.ndjson"
    phase_path = root / f"{state['session_id']}.phase.json"
    claim_path = root / "binding-probes" / f"{state['session_id']}.json"

    def diagnostic_for(suffix: str) -> Path | None:
        if diagnostic_path is None:
            return None
        return diagnostic_path.with_name(f"{diagnostic_path.stem}-{suffix}{diagnostic_path.suffix}")

    try:
        start_bytes = events_path.stat().st_size
    except OSError:
        start_bytes = 0
    try:
        claim = _read_json_bounded(claim_path)
    except (OSError, ValueError, json.JSONDecodeError):
        claim = {}
    if (
        claim.get("schema_version") != 2
        or claim.get("provider") != "cursor"
        or claim.get("status") != "observed"
        or claim.get("session_id") != state.get("session_id")
        or claim.get("conversation_uuid") != state.get("provider_session_id")
        or claim.get("run_id") != state.get("run_id")
        or not claim.get("launch_id")
    ):
        raise RuntimeError(
            "Cursor interrupt recovery lacks the enrolled binding "
            f"(claim_path={claim_path}, binding={json.dumps(_diagnostic_mapping(claim, ('schema_version', 'provider', 'status', 'session_id', 'conversation_uuid', 'launch_id', 'run_id')), sort_keys=True)})"
        )
    try:
        phase = _read_json_bounded(phase_path)
    except (OSError, ValueError, json.JSONDecodeError):
        phase = {}
    expected_generation_id = str(phase.get("generation_id") or "").strip()
    phase_identity_matches = (
        phase.get("session_id") == state.get("session_id")
        and phase.get("conversation_id") == state.get("provider_session_id")
        and phase.get("launch_id") == claim.get("launch_id")
        and bool(expected_generation_id)
    )
    if not phase_identity_matches:
        raise RuntimeError(
            "Cursor interrupt recovery requires the identity-matched generation "
            f"(phase_path={phase_path}, phase={json.dumps(_diagnostic_mapping(phase, ('session_id', 'conversation_id', 'launch_id', 'phase', 'generation_id')), sort_keys=True)})"
        )
    if phase.get("phase") == "idle":
        idle_phase = _wait_cursor_idle(
            state,
            environment,
            timeout=min(timeout, 10.0),
            expected_generation_id=expected_generation_id,
            diagnostic_path=diagnostic_for("already-idle"),
        )
        wait_cursor_tui_ready(process, process.recording, timeout=min(timeout, 30.0))
        return {
            "method": "cursor_native_idle_late",
            "start_bytes": start_bytes,
            "end_bytes": events_path.stat().st_size if events_path.exists() else start_bytes,
            "generation_id": expected_generation_id,
            "idle_phase": idle_phase,
            "observed_events": [],
            "tui_ready": True,
        }
    if phase.get("phase") != "active":
        raise RuntimeError(
            "Cursor interrupt recovery requires an active generation "
            f"(phase_path={phase_path}, phase={json.dumps(_diagnostic_mapping(phase, ('session_id', 'conversation_id', 'launch_id', 'phase', 'generation_id')), sort_keys=True)})"
        )
    # Close the snapshot-to-signal race as far as the provider-owned files
    # allow. If the generation completed between the first read and this
    # final check, preserve the late idle result instead of interrupting a
    # replacement generation.
    try:
        latest_phase = _read_json_bounded(phase_path)
    except (OSError, ValueError, json.JSONDecodeError):
        latest_phase = {}
    if (
        latest_phase.get("session_id") != state.get("session_id")
        or latest_phase.get("conversation_id") != state.get("provider_session_id")
        or latest_phase.get("launch_id") != claim.get("launch_id")
        or latest_phase.get("generation_id") != expected_generation_id
    ):
        raise RuntimeError(
            "Cursor active generation changed before interrupt recovery "
            f"(phase_path={phase_path}, phase={json.dumps(_diagnostic_mapping(latest_phase, ('session_id', 'conversation_id', 'launch_id', 'phase', 'generation_id')), sort_keys=True)})"
        )
    if latest_phase.get("phase") == "idle":
        idle_phase = _wait_cursor_idle(
            state,
            environment,
            timeout=min(timeout, 10.0),
            minimum_hook_event_bytes=start_bytes,
            expected_generation_id=expected_generation_id,
            diagnostic_path=diagnostic_for("late-idle"),
        )
        wait_cursor_tui_ready(process, process.recording, timeout=min(timeout, 30.0))
        return {
            "method": "cursor_native_idle_late",
            "start_bytes": start_bytes,
            "end_bytes": events_path.stat().st_size if events_path.exists() else start_bytes,
            "generation_id": expected_generation_id,
            "idle_phase": idle_phase,
            "observed_events": [],
            "tui_ready": True,
        }
    if latest_phase.get("phase") != "active":
        raise RuntimeError(
            "Cursor active generation ended before interrupt recovery "
            f"(phase_path={phase_path}, phase={json.dumps(_diagnostic_mapping(latest_phase, ('session_id', 'conversation_id', 'launch_id', 'phase', 'generation_id')), sort_keys=True)})"
        )
    process.send("\x03")
    deadline = time.monotonic() + timeout
    stop_event: dict[str, Any] | None = None
    leaked_response = False
    observed_events: list[str] = []
    seen_event_keys: set[str] = set()
    scan_offset = start_bytes
    partial_line = b""
    discard_partial_line = False
    stop_settle_deadline: float | None = None
    while time.monotonic() < deadline or (stop_settle_deadline is not None and time.monotonic() < stop_settle_deadline):
        process.drain()
        try:
            with events_path.open("rb") as stream:
                stream.seek(scan_offset)
                chunk = stream.read(_CURSOR_HOOK_OBSERVATION_BYTES)
                scan_offset += len(chunk)
        except OSError:
            chunk = b""
        if discard_partial_line:
            newline = chunk.find(b"\n")
            if newline < 0:
                time.sleep(0.1)
                continue
            chunk = chunk[newline + 1 :]
            discard_partial_line = False
        raw = partial_line + chunk
        lines = raw.splitlines(keepends=True)
        if lines and not lines[-1].endswith((b"\n", b"\r")):
            partial_line = lines.pop()
            if len(partial_line) > _CURSOR_HOOK_OBSERVATION_BYTES:
                partial_line = b""
                discard_partial_line = True
        else:
            partial_line = b""
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("session_id") != state.get("session_id"):
                continue
            if event.get("conversation_id") != state.get("provider_session_id"):
                continue
            if event.get("launch_id") != claim.get("launch_id"):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("generation_id") != expected_generation_id:
                continue
            event_key = json.dumps(event, sort_keys=True, separators=(",", ":"))
            if event_key in seen_event_keys:
                continue
            seen_event_keys.add(event_key)
            if len(seen_event_keys) > 1024:
                seen_event_keys.clear()
            name = str(event.get("event") or "")
            if name and len(observed_events) < 256:
                observed_events.append(name)
            if name == "afterAgentResponse":
                leaked_response = True
            if name == "stop":
                status = str(payload.get("status") or "").strip().lower()
                is_interrupt = payload.get("is_interrupt") is True
                if status in {"aborted", "cancelled", "error"} or is_interrupt:
                    stop_event = event
                    # Keep scanning briefly after the stop receipt. Cursor's
                    # hooks are append-only and a late response for the same
                    # generation must invalidate this recovery rather than be
                    # hidden by the first terminal event.
                    stop_settle_deadline = time.monotonic() + 2.0
                    continue
                # A late normal completion is safe only if the provider now
                # proves the same generation idle. Otherwise fail closed.
                try:
                    idle_phase = _wait_cursor_idle(
                        state,
                        environment,
                        timeout=2.0,
                        minimum_hook_event_bytes=start_bytes,
                        expected_generation_id=expected_generation_id,
                        diagnostic_path=diagnostic_for("stop-race"),
                    )
                except RuntimeError:
                    diagnostic_payload = {
                        "generation_id": _diagnostic_scalar(payload.get("generation_id")),
                        "status": (
                            payload.get("status")
                            if isinstance(payload.get("status"), str)
                            and payload.get("status") in {"aborted", "cancelled", "completed", "error"}
                            else None
                        ),
                        "is_interrupt": payload.get("is_interrupt") if isinstance(payload.get("is_interrupt"), bool) else None,
                    }
                    raise RuntimeError(
                        "Cursor interrupt recovery observed a non-interrupt stop "
                        f"(generation_id={_diagnostic_scalar(expected_generation_id)}, payload={json.dumps(diagnostic_payload, sort_keys=True)})"
                    ) from None
                wait_cursor_tui_ready(process, process.recording, timeout=min(timeout, 30.0))
                return {
                    "method": "cursor_native_idle_late",
                    "start_bytes": start_bytes,
                    "end_bytes": events_path.stat().st_size if events_path.exists() else start_bytes,
                    "generation_id": expected_generation_id,
                    "stop_hook": event,
                    "idle_phase": idle_phase,
                    "observed_events": observed_events,
                    "tui_ready": True,
                }
        if stop_event is not None and stop_settle_deadline is not None and time.monotonic() >= stop_settle_deadline:
            break
        time.sleep(0.1)
    if (
        leaked_response
        and stop_event is not None
        and (
            str((stop_event.get("payload") or {}).get("status") or "").strip().lower() in {"aborted", "cancelled", "error"}
            or (stop_event.get("payload") or {}).get("is_interrupt") is True
        )
    ):
        raise RuntimeError(
            "Cursor interrupt recovery allowed the stranded generation to publish another response "
            f"(generation_id={_diagnostic_scalar(expected_generation_id)})"
        )
    if stop_event is None:
        diagnostic_written = False
        diagnostic_target: Path | None = None
        if diagnostic_path is not None:
            diagnostic_target = diagnostic_path.with_name(f"{diagnostic_path.stem}-stop-hook-timeout{diagnostic_path.suffix}")
            try:
                stop_observation = _cursor_stop_timeout_observation(
                    state=state,
                    phase_path=phase_path,
                    claim_path=claim_path,
                    hook_events_path=events_path,
                    minimum_hook_event_bytes=start_bytes,
                    expected_generation_id=expected_generation_id,
                    timeout=timeout,
                    observed_events=observed_events,
                )
            except Exception as exc:  # noqa: BLE001 - diagnostics never replace recovery failure
                stop_observation = {
                    "schema": "cursor_stop_timeout_observation_unavailable.v1",
                    "captured_at": now(),
                    "provider": "cursor",
                    "session_id": _diagnostic_scalar(state.get("session_id")),
                    "error_type": type(exc).__name__,
                }
            diagnostic_written = _write_best_effort_json(diagnostic_target, stop_observation)
        diagnostic_detail = ", diagnostic_path=" + str(diagnostic_target) if diagnostic_written and diagnostic_target else ""
        raise RuntimeError(
            "Cursor interrupt recovery did not publish an identity-matched stop hook "
            f"(session_id={_diagnostic_scalar(state.get('session_id'))}, "
            f"generation_id={_diagnostic_scalar(expected_generation_id)}, "
            f"events={_diagnostic_event_names(observed_events, limit=20)!r}{diagnostic_detail})"
        )
    idle_phase = _wait_cursor_idle(
        state,
        environment,
        timeout=min(timeout, 10.0),
        minimum_hook_event_bytes=start_bytes,
        expected_generation_id=expected_generation_id,
        diagnostic_path=diagnostic_for("final"),
    )
    wait_cursor_tui_ready(process, process.recording, timeout=min(timeout, 30.0))
    try:
        end_bytes = events_path.stat().st_size
    except OSError:
        end_bytes = start_bytes
    return {
        "method": "cursor_ctrl_c_recovery",
        "start_bytes": start_bytes,
        "end_bytes": end_bytes,
        "generation_id": expected_generation_id,
        "stop_hook": stop_event,
        "idle_phase": idle_phase,
        "observed_events": observed_events,
        "tui_ready": True,
    }


def _cursor_file_observation(path: Path, *, marker: str) -> dict[str, Any]:
    """Hash a provider file and report marker presence without retaining text."""

    digest = hashlib.sha256()
    marker_bytes = marker.encode("utf-8")
    overlap = b""
    marker_count = 0
    total_bytes = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total_bytes += len(chunk)
                window = overlap + chunk
                marker_count += window.count(marker_bytes)
                overlap = window[-max(0, len(marker_bytes) - 1) :]
        stat = path.stat()
    except OSError as exc:
        return {
            "path": str(path),
            "read_error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "path": str(path),
        "size": total_bytes,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": f"sha256:{digest.hexdigest()}",
        "marker_observed": marker_count > 0,
        "marker_count": marker_count,
    }


def _cursor_sqlite_observation(path: Path, *, marker: str) -> dict[str, Any]:
    """Observe Cursor SQLite metadata in read-only mode, never checkpointing WAL."""

    observation = _cursor_file_observation(path, marker=marker)
    if "read_error" in observation:
        return {"kind": "store_db", **observation}
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            table_counts: dict[str, int] = {}
            for table in ("meta", "blobs"):
                if table in tables:
                    table_counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"kind": "store_db", **observation, "sqlite_error": f"{type(exc).__name__}: {exc}"}
    return {"kind": "store_db", **observation, "sqlite_tables": sorted(tables), "sqlite_counts": table_counts}


def _cursor_engine_db_observation(path: Path) -> dict[str, Any]:
    """Summarize engine state tables without copying transcript or credentials.

    Counts alone cannot distinguish a host-side source-epoch loss from a local
    lineage decision. Keep bounded identity and position rows as diagnostic
    evidence so a retained Cursor failure can be reconstructed without storing
    the compressed request body or transcript payload.
    """

    if not path.exists():
        return {"path": str(path), "present": False}
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            connection.execute("BEGIN DEFERRED")
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            table_counts: dict[str, int] = {}
            table_counts_errors: dict[str, str] = {}
            table_rows: dict[str, list[dict[str, Any]]] = {}
            table_rows_errors: dict[str, str] = {}
            for table in (
                "source_epoch_registry",
                "source_epoch_lane_state",
                "pending_source_envelope",
                "cursor_store_raw_record",
                "cursor_store_capture_cursor",
            ):
                if table in tables:
                    try:
                        table_counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    except sqlite3.Error as exc:
                        table_counts_errors[table] = f"{type(exc).__name__}: {exc}"
            row_specs = {
                "source_epoch_registry": (
                    "source_epoch",
                    "provider",
                    "opaque_source_id",
                    "predecessor_epoch",
                    "start_reason",
                    "max_observed_len",
                    "source_revision",
                    "bound_session_id",
                    "provider_session_id",
                    "file_incarnation",
                    "wake_at",
                    "created_at",
                    "updated_at",
                    "ended_at",
                    "end_reason",
                ),
                "source_epoch_lane_state": ("source_epoch", "lane", "last_position", "updated_at"),
                "pending_source_envelope": (
                    "source_epoch",
                    "source_path",
                    "range_start",
                    "range_end",
                    "envelope_id",
                    "raw_bytes",
                    "event_count",
                    "has_reply_evidence",
                    "has_more",
                    "created_at",
                    "attempt_count",
                    "last_attempt_at",
                    "blocked_at",
                    "block_kind",
                    "block_detail",
                ),
                "cursor_store_raw_record": ("source_epoch", "source_position", "record_hash"),
                "cursor_store_capture_cursor": ("source_epoch", "last_blob_id", "updated_at"),
            }
            for table, requested_columns in row_specs.items():
                if table not in tables:
                    continue
                try:
                    available = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
                    columns = [column for column in requested_columns if column in available]
                    if not columns:
                        continue
                    select_columns = ", ".join(columns)
                    try:
                        rows = connection.execute(f"SELECT {select_columns} FROM {table} ORDER BY rowid DESC LIMIT 128").fetchall()
                    except sqlite3.Error:
                        rows = connection.execute(f"SELECT {select_columns} FROM {table} LIMIT 128").fetchall()
                    serialized_rows: list[dict[str, Any]] = []
                    for row in rows:
                        serialized = dict(zip(columns, row, strict=True))
                        raw_detail = serialized.get("block_detail")
                        if isinstance(raw_detail, str) and not _SAFE_DIAGNOSTIC_DETAIL_RE.fullmatch(raw_detail):
                            serialized["block_detail_sha256"] = hashlib.sha256(raw_detail.encode()).hexdigest()
                            serialized["block_detail_length"] = len(raw_detail)
                            serialized["block_detail"] = "<omitted>"
                        serialized_rows.append(serialized)
                    table_rows[table] = serialized_rows
                except sqlite3.Error as exc:
                    table_rows_errors[table] = f"{type(exc).__name__}: {exc}"
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"path": str(path), "present": True, "sqlite_error": f"{type(exc).__name__}: {exc}"}
    observation = {
        "path": str(path),
        "present": True,
        "sqlite_tables": sorted(tables),
        "sqlite_counts": table_counts,
        "table_rows": table_rows,
    }
    if table_rows_errors:
        observation["table_rows_errors"] = table_rows_errors
    if table_counts_errors:
        observation["table_counts_errors"] = table_counts_errors
    return observation


def _cursor_managed_state_observation(root: Path, session_id: str) -> list[dict[str, Any]]:
    """Retain only identity fields from the reservation and binding probes."""

    observations: list[dict[str, Any]] = []
    for directory in (root / "launch-reservations", root / "binding-probes"):
        path = directory / f"{session_id}.json"
        try:
            payload = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        observations.append(
            {
                "path": str(path),
                "fields": {
                    key: payload.get(key)
                    for key in (
                        "schema_version",
                        "provider",
                        "status",
                        "session_id",
                        "conversation_uuid",
                        "launch_id",
                        "run_id",
                        "owner_pid",
                        "owner_start_time",
                    )
                    if key in payload
                },
            }
        )
    return observations


def _cursor_projection_diagnostics(
    *,
    environment: dict[str, str],
    state: dict[str, Any],
    marker: str,
    engine_db_path: Path,
    phase: str,
) -> dict[str, Any]:
    """Capture the minimum evidence needed to locate a Cursor projection gap."""

    provider_session_id = str(state.get("provider_session_id") or "").strip()
    home = Path(str(environment.get("HOME") or "")).expanduser()
    cursor_home = Path(str(environment.get("CURSOR_HOME") or home / ".cursor")).expanduser()
    longhouse_home = Path(str(environment.get("LONGHOUSE_HOME") or home / ".longhouse")).expanduser()
    roots = (cursor_home / "chats", cursor_home / "projects", home / ".config" / "cursor" / "chats")
    files: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = list(root.rglob("store.db")) + [path for path in root.rglob("*.jsonl") if "agent-transcripts" in path.parts]
        except OSError:
            continue
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            if provider_session_id and provider_session_id not in str(path):
                if path.name == "store.db":
                    try:
                        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)
                        try:
                            row = connection.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
                        finally:
                            connection.close()
                    except sqlite3.Error:
                        row = None
                    if not row:
                        continue
                else:
                    continue
            if path.name == "store.db":
                files.append(_cursor_sqlite_observation(path, marker=marker))
                # SQLite may leave the newest committed pages in an active
                # WAL. Retain a metadata-only observation of that sidecar so
                # a missing marker in the main file is not misread as proof
                # that Cursor never persisted it.
                wal_path = path.with_name(f"{path.name}-wal")
                if wal_path.is_file():
                    wal_observation = _cursor_file_observation(wal_path, marker=marker)
                    wal_observation["kind"] = "store_db_wal"
                    files.append(wal_observation)
            else:
                observation = _cursor_file_observation(path, marker=marker)
                observation["kind"] = "agent_transcript"
                files.append(observation)
    return {
        "schema": "cursor_projection_diagnostics.v2",
        "phase": phase,
        "captured_at": now(),
        "provider": "cursor",
        "session_id": state.get("session_id"),
        "provider_session_id": provider_session_id,
        "marker": marker,
        "files": files,
        "managed_state": _cursor_managed_state_observation(
            longhouse_home / "managed-local" / "cursor-helm",
            str(state.get("session_id") or ""),
        ),
        "engine_db": _cursor_engine_db_observation(engine_db_path),
    }


_CURSOR_HOOK_OBSERVATION_BYTES = 65536


def _diagnostic_scalar(value: Any, *, limit: int = 128) -> str | bool | None:
    """Keep provider-controlled diagnostic scalars bounded and non-secret-shaped."""

    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value)[:limit]
    if not _SAFE_DIAGNOSTIC_DETAIL_RE.fullmatch(text.lower()):
        return None
    return text


def _diagnostic_mapping(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str | bool]:
    """Project provider JSON to bounded, syntax-safe diagnostic scalars."""

    result: dict[str, str | bool] = {}
    for key in keys:
        value = _diagnostic_scalar(payload.get(key))
        if value is not None:
            result[key] = value
    return result


def _diagnostic_event_names(values: list[str], *, limit: int = 32) -> list[str]:
    return [value for value in (_diagnostic_scalar(item) for item in values[-limit:]) if isinstance(value, str)]


def _cursor_hook_event_observation(
    path: Path,
    *,
    state: dict[str, Any],
    minimum_bytes: int | None,
    expected_launch_id: str | None = None,
    expected_generation_id: str | None = None,
) -> dict[str, Any]:
    """Summarize native hook history without retaining prompt or response text."""

    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        return {
            "path": str(path),
            "present": False,
            "size_bytes": None,
            "read_error": type(exc).__name__,
            "minimum_bytes": minimum_bytes,
            "events": [],
            "truncated": False,
            "undecodable_lines": 0,
            "malformed_lines": 0,
        }
    start_offset = min(max(int(minimum_bytes or 0), 0), size_bytes)
    available_bytes = max(size_bytes - start_offset, 0)
    expected_launch = str(expected_launch_id or "").strip()
    expected_generation = str(expected_generation_id or "").strip()
    truncated = available_bytes > _CURSOR_HOOK_OBSERVATION_BYTES
    try:
        with path.open("rb") as stream:
            if not truncated:
                stream.seek(start_offset)
                encoded = stream.read(available_bytes)
                chunks = (encoded,)
            else:
                first_bytes = _CURSOR_HOOK_OBSERVATION_BYTES // 2
                last_bytes = _CURSOR_HOOK_OBSERVATION_BYTES - first_bytes
                stream.seek(start_offset)
                first = stream.read(first_bytes)
                stream.seek(size_bytes - last_bytes)
                last = stream.read(last_bytes)
                chunks = (first, last)
    except OSError as exc:
        return {
            "path": str(path),
            "present": True,
            "size_bytes": size_bytes,
            "read_error": type(exc).__name__,
            "minimum_bytes": minimum_bytes,
            "events": [],
            "truncated": truncated,
            "undecodable_lines": 0,
            "malformed_lines": 0,
        }

    lines: list[bytes] = []
    if truncated:
        first_lines = chunks[0].splitlines()
        last_lines = chunks[1].splitlines()
        if first_lines and not chunks[0].endswith(b"\n"):
            first_lines.pop()
        if last_lines and not chunks[1].startswith(b"\n"):
            last_lines.pop(0)
        lines.extend(first_lines)
        lines.extend(last_lines)
    else:
        lines.extend(chunks[0].splitlines())

    events: list[dict[str, Any]] = []
    mismatch_event_count = 0
    mismatch_event_names: list[str] = []
    undecodable_lines = 0
    malformed_lines = 0
    selected_lines = lines if len(lines) <= 32 else [*lines[:16], *lines[-16:]]
    for line in selected_lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except UnicodeDecodeError:
            undecodable_lines += 1
            continue
        except (json.JSONDecodeError, TypeError):
            malformed_lines += 1
            continue
        if not isinstance(event, dict):
            malformed_lines += 1
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        session_match = event.get("session_id") == state.get("session_id")
        conversation_match = event.get("conversation_id") == state.get("provider_session_id")
        launch_match = bool(expected_launch) and event.get("launch_id") == expected_launch
        generation_match = bool(expected_generation) and payload.get("generation_id") == expected_generation
        identity_match = session_match and conversation_match and launch_match and generation_match
        if not identity_match:
            mismatch_event_count += 1
            if len(mismatch_event_names) < 16:
                event_name = _diagnostic_scalar(event.get("event"))
                if isinstance(event_name, str):
                    mismatch_event_names.append(event_name)
            continue
        events.append(
            {
                "event": _diagnostic_scalar(event.get("event")),
                "observed_at": _diagnostic_scalar(event.get("observed_at") or event.get("captured_at") or event.get("timestamp")),
                "session_id": _diagnostic_scalar(event.get("session_id")),
                "conversation_id": _diagnostic_scalar(event.get("conversation_id")),
                "launch_id": _diagnostic_scalar(event.get("launch_id")),
                "identity_match": identity_match,
                "generation_id": _diagnostic_scalar(payload.get("generation_id")),
                "payload_keys": sorted(key for key in payload if key in _CURSOR_DIAGNOSTIC_PAYLOAD_KEYS),
                "status": (
                    _diagnostic_scalar(payload.get("status"))
                    if isinstance(payload.get("status"), str) and payload.get("status") in {"aborted", "cancelled", "completed", "error"}
                    else None
                ),
                "phase": (
                    _diagnostic_scalar(payload.get("phase"))
                    if isinstance(payload.get("phase"), str) and payload.get("phase") in {"active", "idle"}
                    else None
                ),
                "is_interrupt": _diagnostic_scalar(payload.get("is_interrupt")),
            }
        )
    return {
        "path": str(path),
        "present": True,
        "size_bytes": size_bytes,
        "minimum_bytes": minimum_bytes,
        "available_bytes": available_bytes,
        "truncated": truncated,
        "undecodable_lines": undecodable_lines,
        "malformed_lines": malformed_lines,
        "mismatch_event_count": mismatch_event_count,
        "mismatch_event_names": mismatch_event_names,
        "events": events,
    }


def _cursor_idle_timeout_observation(
    *,
    state: dict[str, Any],
    phase_path: Path,
    claim_path: Path,
    hook_events_path: Path,
    minimum_hook_event_bytes: int | None,
    expected_generation_id: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Capture the causal native lifecycle boundary when idle never arrives."""

    try:
        phase = _read_json_bounded(phase_path)
    except (OSError, ValueError, json.JSONDecodeError):
        phase = {}
    try:
        claim = _read_json_bounded(claim_path)
    except (OSError, ValueError, json.JSONDecodeError):
        claim = {}
    identity_keys = (
        "schema_version",
        "provider",
        "status",
        "session_id",
        "conversation_uuid",
        "launch_id",
        "run_id",
    )
    phase_keys = ("session_id", "conversation_id", "launch_id", "phase", "generation_id")
    return {
        "schema": "cursor_idle_timeout_observation.v1",
        "captured_at": now(),
        "provider": "cursor",
        "session_id": _diagnostic_scalar(state.get("session_id")),
        "provider_session_id": _diagnostic_scalar(state.get("provider_session_id")),
        "wait": {
            "timeout_seconds": timeout,
            "minimum_hook_event_bytes": minimum_hook_event_bytes,
            "expected_generation_id": _diagnostic_scalar(expected_generation_id),
        },
        "phase_file": _diagnostic_mapping(phase, phase_keys),
        "binding": _diagnostic_mapping(claim, identity_keys),
        "hook_events": _cursor_hook_event_observation(
            hook_events_path,
            state=state,
            minimum_bytes=minimum_hook_event_bytes,
            expected_launch_id=claim.get("launch_id") or phase.get("launch_id"),
            expected_generation_id=expected_generation_id or phase.get("generation_id"),
        ),
    }


def _cursor_stop_timeout_observation(
    *,
    state: dict[str, Any],
    phase_path: Path,
    claim_path: Path,
    hook_events_path: Path,
    minimum_hook_event_bytes: int,
    expected_generation_id: str,
    timeout: float,
    observed_events: list[str],
) -> dict[str, Any]:
    """Capture the causal boundary when Ctrl-C yields no native stop hook."""

    observation = _cursor_idle_timeout_observation(
        state=state,
        phase_path=phase_path,
        claim_path=claim_path,
        hook_events_path=hook_events_path,
        minimum_hook_event_bytes=minimum_hook_event_bytes,
        expected_generation_id=expected_generation_id,
        timeout=timeout,
    )
    observation["schema"] = "cursor_stop_timeout_observation.v1"
    observation["stop"] = {
        "start_hook_event_bytes": minimum_hook_event_bytes,
        "expected_generation_id": _diagnostic_scalar(expected_generation_id),
        "stop_hook_observed": False,
        "observed_events": _diagnostic_event_names(observed_events),
    }
    return observation


def _wait_cursor_idle(
    state: dict[str, Any],
    environment: dict[str, str],
    *,
    timeout: float = 45.0,
    minimum_hook_event_bytes: int | None = None,
    expected_generation_id: str | None = None,
    diagnostic_path: Path | None = None,
) -> dict[str, Any]:
    """Wait for the provider-owned Cursor hook to publish an idle phase."""

    longhouse_home = str(environment.get("LONGHOUSE_HOME") or "").strip()
    if not longhouse_home:
        raise RuntimeError("Cursor Helm qualification has no explicit Longhouse home")
    root = Path(longhouse_home) / "managed-local" / "cursor-helm"
    path = root / f"{state['session_id']}.phase.json"
    claim_path = root / "binding-probes" / f"{state['session_id']}.json"
    hook_events_path = root / "hook-events" / f"{state['session_id']}.ndjson"
    deadline = time.monotonic() + timeout
    last_identity: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            payload = _read_json_bounded(path)
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
        try:
            claim = _read_json_bounded(claim_path)
        except (OSError, ValueError, json.JSONDecodeError):
            claim = {}
        last_identity = {
            "phase": _diagnostic_mapping(payload, ("session_id", "conversation_id", "launch_id", "phase", "generation_id")),
            "binding": _diagnostic_mapping(
                claim,
                ("schema_version", "provider", "status", "session_id", "conversation_uuid", "launch_id", "run_id"),
            ),
        }
        binding_matches = (
            claim.get("schema_version") == 2
            and claim.get("provider") == "cursor"
            and claim.get("status") == "observed"
            and claim.get("session_id") == state.get("session_id")
            and claim.get("conversation_uuid") == state.get("provider_session_id")
            and claim.get("run_id") == state.get("run_id")
            and bool(claim.get("launch_id"))
        )
        if minimum_hook_event_bytes is not None:
            try:
                hook_event_bytes = hook_events_path.stat().st_size
            except OSError:
                hook_event_bytes = 0
            if hook_event_bytes <= minimum_hook_event_bytes:
                time.sleep(0.25)
                continue
        if (
            payload.get("session_id") == state.get("session_id")
            and payload.get("conversation_id") == state.get("provider_session_id")
            and payload.get("launch_id") == claim.get("launch_id")
            and payload.get("phase") == "idle"
            and (expected_generation_id is None or payload.get("generation_id") == expected_generation_id)
            and binding_matches
        ):
            return payload
        time.sleep(0.25)
    try:
        timeout_observation = _cursor_idle_timeout_observation(
            state=state,
            phase_path=path,
            claim_path=claim_path,
            hook_events_path=hook_events_path,
            minimum_hook_event_bytes=minimum_hook_event_bytes,
            expected_generation_id=expected_generation_id,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics never replace the timeout verdict
        timeout_observation = {
            "schema": "cursor_idle_timeout_observation_unavailable.v1",
            "captured_at": now(),
            "provider": "cursor",
            "session_id": _diagnostic_scalar(state.get("session_id")),
            "error_type": type(exc).__name__,
        }
    diagnostic_written = False
    if diagnostic_path is not None:
        diagnostic_written = _write_best_effort_json(diagnostic_path, timeout_observation)
    diagnostic_detail = f", diagnostic_path={diagnostic_path}" if diagnostic_written else ""
    raise RuntimeError(
        "Cursor native hooks did not publish an identity-matched idle phase "
        f"(phase_path={path}, claim_path={claim_path}{diagnostic_detail}, "
        f"identity={json.dumps(last_identity, sort_keys=True)})"
    )


def _control_send(
    spec: ProviderSpec,
    args: argparse.Namespace,
    state: dict[str, Any],
    process: PtyProcess,
    text: str,
    *,
    initial: bool = False,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    if spec.provider == "claude":
        command = [str(args.engine), "claude-channel", "send", "--session-id", state["session_id"], "--text", text]
    elif spec.provider == "cursor" and initial:
        # Cursor does not emit its first lifecycle hook until the first
        # foreground prompt is submitted.  The managed socket intentionally
        # refuses a send without that provider-owned idle phase. Use one
        # disposable PTY bootstrap message to make Cursor publish the native
        # hook evidence; every message after bootstrap (including Resume) uses
        # the authoritative Helm socket below.
        if process.process.poll() is not None:
            raise RuntimeError("cursor terminal control owner is no longer live")
        # Keep this byte sequence aligned with the native Cursor Helm socket:
        # text, an escape to dismiss its completion overlay, then Enter. A
        # single `text\\r` write can leave the text visible in the input box
        # without submitting the foreground prompt.
        process.send(text)
        time.sleep(0.3)
        process.send("\x1b")
        time.sleep(0.1)
        process.send("\r")
        return {"method": "provider_tty_bootstrap", "returncode": 0}
    elif spec.provider == "cursor":
        if environment is None:
            raise RuntimeError("Cursor managed control requires the qualification environment")
        command = [str(args.engine), "cursor-helm", "send", "--session-id", state["session_id"], "--text", text]
    else:
        if process.process.poll() is not None:
            raise RuntimeError(f"{spec.provider} terminal control owner is no longer live")
        process.send(text + "\r")
        return {"method": "provider_tty_bootstrap" if initial else "provider_tty", "returncode": 0}
    # Cursor can publish its completed-turn hook before the TUI has returned
    # to the provider-owned idle phase.  Keep the authoritative socket retry
    # bounded, but give that transition the same live-send budget as the rest
    # of the Resume canary instead of failing after a fixed 30-second window.
    cursor_send_timeout = float(getattr(args, "live_send_timeout_secs", 30))
    deadline = time.monotonic() + cursor_send_timeout if spec.provider == "cursor" else time.monotonic()
    attempts = 0
    completed: subprocess.CompletedProcess[str]
    while True:
        attempts += 1
        completed = subprocess.run(
            command,
            cwd=args.repo_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
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


def cursor_bootstrap_prompt(marker: str = "READY") -> str:
    """Return a side-effect-free first-turn prompt for Cursor's hook probe.

    Cursor's native lifecycle hooks are not observed until the first foreground
    prompt is submitted through its PTY.  The prompt only establishes that
    provider-owned boundary; the actual qualification marker is sent through
    the managed Helm socket after the hook reports idle.
    """

    # Cursor's native hook path has a provider-specific parsing quirk: the
    # proven wording uses "with exactly".  The shorter "Reply exactly" form
    # emits beforeSubmitPrompt but can leave the stock TUI in Working without
    # publishing the provider-owned idle boundary.  Keep the bootstrap free of
    # extra prohibitions while retaining the known-good instruction shape.
    return f"Reply with exactly {marker}"


def stop_session(
    spec: ProviderSpec,
    args: argparse.Namespace,
    state: dict[str, Any],
    process: PtyProcess,
    *,
    force: bool,
    environment: dict[str, str] | None = None,
    stop_phase: str = "initial",
) -> dict[str, Any]:
    pid = process.pid
    provider_pid = provider_process_pid(spec, state)
    control_returncode: int | None = None
    if force:
        process.kill_group(signal.SIGKILL)
        method = "sigkill_exact_owner_group"
    elif spec.provider == "cursor":
        # `cursor-helm stop` is the product terminate operation and
        # intentionally SIGKILLs the provider. The clean-exit assurance cell
        # must exercise Cursor's own normal TUI shutdown instead, so submit
        # its supported `/exit` command through the managed Helm socket and
        # reserve the terminate operation for the process-loss variant.
        if environment is None:
            raise RuntimeError("Cursor clean stop requires the qualification environment")
        evidence_root = getattr(args, "evidence_root", None)
        evidence_root = Path(evidence_root).resolve() if evidence_root else None
        phase_label = stop_phase if stop_phase in {"initial", "final"} else "initial"
        idle_diagnostic_path = Path(evidence_root) / f"cursor-idle-timeout-clean-stop-{phase_label}.json" if evidence_root else None
        recovery_diagnostic_path = (
            Path(evidence_root) / f"cursor-idle-timeout-clean-stop-{phase_label}-recovery.json" if evidence_root else None
        )
        try:
            _wait_cursor_idle(
                state,
                environment,
                timeout=min(float(getattr(args, "live_send_timeout_secs", 30)), 15.0),
                **({"diagnostic_path": idle_diagnostic_path} if idle_diagnostic_path is not None else {}),
            )
            cursor_recovery = {"method": "cursor_native_idle", "tui_ready": True}
        except RuntimeError as idle_error:
            # The marker was already correlated by run_native_resume before
            # teardown. If Cursor leaves its UI in Thinking anyway, recover
            # through the provider's normal interrupt path, prove the exact
            # generation stopped, and only then send the supported `/exit`.
            cursor_recovery = _cursor_interrupt_to_idle(
                state,
                environment,
                process,
                timeout=float(getattr(args, "live_send_timeout_secs", 30)),
                **({"diagnostic_path": recovery_diagnostic_path} if recovery_diagnostic_path is not None else {}),
            )
            cursor_recovery["idle_wait_error"] = f"{type(idle_error).__name__}: {idle_error}"
        control_result = _control_send(
            spec,
            args,
            state,
            process,
            "/exit",
            environment=environment,
        )
        control_returncode = int(control_result["returncode"])
        method = "cursor_native_exit"
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
        if spec.provider == "claude":
            # Claude's TUI asks for a second EOF after the first one; without
            # it the harness falls back to SIGTERM and the clean-exit cell is
            # indistinguishable from a forced process loss.
            time.sleep(0.2)
            process.send("\x04")
        method = "claude_terminal_eof"
    # Cursor's native `/exit` is acknowledged before the outer Helm process
    # has necessarily finished its shutdown. A slow but graceful provider
    # teardown must not become a synthetic SIGTERM failure at the old 30s
    # boundary. Keep this bounded, while allowing the provider enough time to
    # reap its native child and close the managed PTY.
    exit_wait_timeout = 90 if spec.provider == "cursor" and not force else (30 if spec.provider == "opencode" else 10)
    exit_code = process.wait(exit_wait_timeout)
    fallback_signal: str | None = None
    if exit_code is None:
        fallback_signal = "SIGKILL" if force else "SIGTERM"
        process.kill_group(signal.SIGKILL if force else signal.SIGTERM)
        exit_code = process.wait(5)
    group_dead = wait_process_group_dead(pid)
    provider_force_signal_sent = force and _signal_pid_if_alive(provider_pid, signal.SIGKILL)
    provider_process_dead = wait_pid_dead(provider_pid)
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
        "cursor_recovery": cursor_recovery if spec.provider == "cursor" and not force else None,
        "dead": dead,
        "clean": clean,
    }


def launch_command(
    spec: ProviderSpec,
    args: argparse.Namespace,
    session_id: str | None,
    *,
    use_credential_files: bool = False,
    cwd: Path | None = None,
    prompt: str | None = None,
) -> list[str]:
    working_directory = cwd or args.repo_root
    command = [
        str(args.longhouse_cli),
        spec.provider,
        "--cwd",
        str(working_directory),
        "--url",
        args.api_url,
    ]
    if not use_credential_files:
        command.extend(("--token", args.agents_token))
    command.extend((spec.binary_flag, str(args.provider_bin)))
    if spec.provider == "cursor":
        command.extend(("--permission-mode", "auto_approve"))
    if spec.provider == "opencode":
        opencode_model = os.environ.get("LONGHOUSE_OPENCODE_MODEL", "").strip()
        if opencode_model:
            command.extend(("--model", opencode_model))
    if session_id is not None:
        command.extend((spec.resume_flag, session_id))
    if spec.provider == "cursor":
        cursor_model = os.environ.get("CURSOR_MODEL", "").strip()
        if cursor_model:
            command.extend(("--", "--model", cursor_model))
        if prompt:
            # Cursor accepts a first prompt after its provider flags.  Using
            # that native launch surface gives the provider a complete turn
            # to publish its afterAgentResponse/idle hook before the Helm
            # socket is used for the qualification marker.
            command.append(prompt)
    return command


def secret_scan(root: Path, secrets: list[str]) -> list[str]:
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


def isolated_provider_home() -> Path:
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


def initialize_cursor_workspace(path: Path) -> None:
    """Give Cursor the project identity it requires before loading hooks.

    Cursor Agent's project-level hook loader does not activate for an arbitrary
    empty directory.  The qualification workspace is intentionally disposable,
    but it still needs to look like the kind of project a real Cursor session
    opens.  Initializing only the local Git metadata keeps the provider profile
    and the checked-out Longhouse source isolated while making that prerequisite
    explicit in the harness.
    """

    environment = os.environ.copy()
    # A qualification project must not inherit a developer's Git template
    # hooks or system-level init policy. Cursor only needs the project marker;
    # the harness must not execute arbitrary host hooks while creating it.
    environment.pop("GIT_TEMPLATE_DIR", None)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    completed = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"Cursor qualification workspace could not initialize Git: {detail}")
