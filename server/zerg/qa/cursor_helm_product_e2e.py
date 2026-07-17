"""Release canary for the real Longhouse Cursor Helm product boundary."""

from __future__ import annotations

import argparse
import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.services.longhouse_paths import get_managed_local_dir
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _wait_until(predicate, *, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {description}")


def _state_ids(root: Path) -> set[str]:
    result: set[str] = set()
    for path in root.glob("*.json"):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        session_id = str(row.get("session_id") or "")
        if session_id and "socket_path" in row:
            result.add(session_id)
    return result


def _hook_rows(root: Path, session_id: str) -> list[dict[str, Any]]:
    path = root / "hook-events" / f"{session_id}.ndjson"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            outer = json.loads(line)
        except ValueError:
            continue
        payload = dict(outer.get("payload") or {})
        payload["event"] = outer.get("event")
        payload["observed_at"] = outer.get("observed_at")
        rows.append(payload)
    return rows


def _assistant_texts(payload: dict[str, Any]) -> list[str]:
    return [str(row.get("content_text") or "") for row in payload.get("events", []) if row.get("role") == "assistant"]


def _pending_pause(payload: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (row for row in payload.get("requests", []) if row.get("status") == "pending" and row.get("can_respond") is True),
        None,
    )


def _response_observed_at(rows: list[dict[str, Any]], marker: str) -> datetime:
    row = next(
        (item for item in reversed(rows) if item.get("event") == "afterAgentResponse" and marker in str(item.get("text") or "")),
        None,
    )
    if row is None:
        raise RuntimeError(f"native Cursor response hook missing for {marker}")
    return datetime.fromisoformat(str(row["observed_at"]))


@dataclass
class _PtyProcess:
    process: subprocess.Popen[bytes]
    master_fd: int
    terminal_path: Path
    stop: threading.Event
    reader: threading.Thread

    @classmethod
    def start(cls, argv: list[str], *, cwd: Path, terminal_path: Path) -> _PtyProcess:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        stop = threading.Event()

        def read_terminal() -> None:
            with terminal_path.open("wb") as output:
                while not stop.is_set():
                    try:
                        chunk = os.read(master_fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.write(chunk)
                    output.flush()

        reader = threading.Thread(target=read_terminal, daemon=True)
        reader.start()
        return cls(process, master_fd, terminal_path, stop, reader)

    def close(self) -> None:
        self.stop.set()
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.reader.join(timeout=2)


def _engine_command(engine: str, session_id: str, kind: str, text: str | None = None) -> None:
    argv = [engine, "cursor-helm", kind, "--session-id", session_id]
    if text is not None:
        argv.extend(["--text", text])
    result = subprocess.run(argv, text=True, capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())


def _restart_machine_agent(status_path: Path, *, timeout: float) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("Machine Agent restart qualification currently requires launchctl on macOS")
    try:
        before = json.loads(status_path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Machine Agent status unavailable at {status_path}") from exc
    old_pid = int(before.get("daemon_pid") or 0)
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.longhouse.shipper"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Machine Agent restart failed").strip())

    def reconnected() -> dict[str, Any] | None:
        try:
            current = json.loads(status_path.read_text())
        except (OSError, ValueError):
            return None
        channel = current.get("control_channel") or {}
        new_pid = int(current.get("daemon_pid") or 0)
        if new_pid > 0 and new_pid != old_pid and channel.get("status") == "connected":
            return current
        return None

    current = _wait_until(reconnected, timeout=timeout, description="Machine Agent restart and control reconnect")
    return {"old_pid": old_pid, "new_pid": int(current["daemon_pid"]), "control_status": "connected"}


def _can_send_live(payload: dict[str, Any]) -> bool:
    capabilities = payload.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("can_send_input") is True


def _session_locked(response: httpx.Response) -> bool:
    if response.status_code != 409:
        return False
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, dict) and detail.get("code") == "SESSION_LOCKED"


def run_product_e2e(args: argparse.Namespace) -> dict[str, Any]:
    longhouse = shutil.which(args.longhouse_bin)
    engine = shutil.which(args.engine_bin)
    if not longhouse or not engine:
        raise RuntimeError("installed longhouse and longhouse-engine binaries are required")
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    root = get_managed_local_dir("cursor-helm")
    before_ids = _state_ids(root)
    artifact_root = args.artifact_root or (
        Path.home() / ".longhouse" / "canaries" / "provider-live" / "cursor-product" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    marker_one = f"LONGHOUSE_CURSOR_PRODUCT_ONE_{uuid4().hex[:10]}"
    marker_two = f"LONGHOUSE_CURSOR_PRODUCT_TWO_{uuid4().hex[:10]}"
    restart_marker = f"LONGHOUSE_CURSOR_PRODUCT_AGENT_RESTART_{uuid4().hex[:10]}"
    recovery = f"LONGHOUSE_CURSOR_PRODUCT_RECOVERY_{uuid4().hex[:10]}"
    forbidden = f"LONGHOUSE_CURSOR_PRODUCT_CANCELLED_{uuid4().hex[:10]}"
    permission_allow = f"LONGHOUSE_CURSOR_PERMISSION_ALLOW_{uuid4().hex[:10]}"
    allow_path = Path("/tmp") / permission_allow
    deny_path = Path("/tmp") / f"LONGHOUSE_CURSOR_PERMISSION_DENY_{uuid4().hex[:10]}"
    terminal_path = artifact_root / "terminal.raw"
    session: _PtyProcess | None = None
    session_id: str | None = None
    report: dict[str, Any] = {"started_at": _now(), "status": "running", "artifact_root": str(artifact_root)}
    try:
        session = _PtyProcess.start(
            [
                longhouse,
                "cursor",
                "--cwd",
                str(workspace),
                "--permission-mode",
                "remote_approve",
                "--",
                "--model",
                args.model,
                f"Reply with exactly {marker_one}",
            ],
            cwd=workspace,
            terminal_path=terminal_path,
        )

        def new_state() -> dict[str, Any] | None:
            for candidate in _state_ids(root) - before_ids:
                try:
                    row = json.loads((root / f"{candidate}.json").read_text())
                except (OSError, ValueError):
                    continue
                if row.get("ready") is True:
                    return row
            return None

        state = _wait_until(new_state, timeout=args.timeout, description="Cursor Helm managed state")
        session_id = str(state["session_id"])
        report["session_id"] = session_id
        claim_path = root / "binding-probes" / f"{session_id}.json"
        claim = _wait_until(
            lambda: json.loads(claim_path.read_text()) if claim_path.exists() else None,
            timeout=args.timeout,
            description="native Cursor binding claim",
        )
        url = get_zerg_url().rstrip("/")
        token = load_token()
        headers = {"X-Agents-Token": token}

        def api_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
            try:
                response = httpx.get(f"{url}{path}", headers=headers, params=params, timeout=10)
            except httpx.TransportError:
                return None
            if response.status_code in {404, 429, 503}:
                return None
            response.raise_for_status()
            return response.json()

        def pending_pause() -> dict[str, Any] | None:
            payload = api_get(f"/api/agents/sessions/{session_id}/pause-requests")
            return _pending_pause(payload) if payload else None

        def answer_pause(pause: dict[str, Any], decision: str) -> dict[str, Any]:
            response = httpx.post(
                f"{url}/api/agents/sessions/{session_id}/pause-requests/{pause['id']}/response",
                headers=headers,
                json={"decision": decision, "message": f"Cursor product canary: {decision}"},
                timeout=10,
            )
            response.raise_for_status()
            return response.json()

        def send_live(text: str) -> dict[str, Any]:
            lock_deadline = time.monotonic() + min(args.timeout, 20.0)
            while True:
                response = httpx.post(
                    f"{url}/api/agents/sessions/{session_id}/send-live",
                    headers=headers,
                    json={"message": text},
                    timeout=30,
                )
                if not _session_locked(response) or time.monotonic() >= lock_deadline:
                    break
                time.sleep(0.5)
            if response.is_error:
                raise RuntimeError(f"Runtime Host send-live failed HTTP {response.status_code}: {response.text[:1000]}")
            payload = response.json()
            if payload.get("accepted") is not True:
                raise RuntimeError(f"Runtime Host did not accept Cursor send: {payload}")
            return payload

        def interrupt_live() -> dict[str, Any]:
            response = httpx.post(
                f"{url}/api/agents/sessions/{session_id}/interrupt-live",
                headers=headers,
                timeout=30,
            )
            if response.is_error:
                raise RuntimeError(f"Runtime Host interrupt-live failed HTTP {response.status_code}: {response.text[:1000]}")
            payload = response.json()
            if payload.get("interrupt_dispatched") is not True:
                raise RuntimeError(f"Runtime Host did not dispatch Cursor interrupt: {payload}")
            return payload

        def hosted_events() -> dict[str, Any] | None:
            return api_get(
                f"/api/agents/sessions/{session_id}/events",
                params={"context_mode": "forensic", "branch_mode": "head", "limit": 100},
            )

        first = _wait_until(
            lambda: (payload if marker_one in _assistant_texts(payload) else None) if (payload := hosted_events()) else None,
            timeout=args.timeout,
            description="first Cursor reply in hosted archive",
        )
        first_archive_lag = (datetime.now(UTC) - _response_observed_at(_hook_rows(root, session_id), marker_one)).total_seconds()
        _wait_until(
            lambda: (payload if _can_send_live(payload) else None) if (payload := api_get(f"/api/agents/sessions/{session_id}")) else None,
            timeout=args.timeout,
            description="Cursor live-control lease on Runtime Host",
        )
        send_live(f"Reply with exactly {marker_two}")
        second = _wait_until(
            lambda: (payload if marker_two in _assistant_texts(payload) else None) if (payload := hosted_events()) else None,
            timeout=args.timeout,
            description="remote Cursor reply in hosted archive",
        )
        second_archive_lag = (datetime.now(UTC) - _response_observed_at(_hook_rows(root, session_id), marker_two)).total_seconds()

        machine_agent_restart = None
        restart_archive_lag = None
        if not args.skip_machine_agent_restart:
            status_path = root.parents[1] / "agent" / "engine-status.json"
            machine_agent_restart = _restart_machine_agent(status_path, timeout=args.timeout)
            if session.process.poll() is not None:
                raise RuntimeError("Cursor TUI exited during Machine Agent restart")
            send_live(f"Reply with exactly {restart_marker}")
            _wait_until(
                lambda: (payload if restart_marker in _assistant_texts(payload) else None) if (payload := hosted_events()) else None,
                timeout=args.timeout,
                description="post-Machine-Agent-restart Cursor reply in hosted archive",
            )
            restart_archive_lag = (datetime.now(UTC) - _response_observed_at(_hook_rows(root, session_id), restart_marker)).total_seconds()

        send_live(
            f"Use the Shell tool to run exactly `touch {allow_path}`, then reply with exactly {permission_allow}",
        )
        allow_pause = _wait_until(pending_pause, timeout=args.timeout, description="hosted Cursor allow request")
        if allow_path.exists():
            raise RuntimeError("Cursor command ran before remote permission approval")
        answer_pause(allow_pause, "answer")
        _wait_until(allow_path.exists, timeout=args.timeout, description="approved Cursor command side effect")
        _wait_until(
            lambda: (payload if permission_allow in _assistant_texts(payload) else None) if (payload := hosted_events()) else None,
            timeout=args.timeout,
            description="approved Cursor response in hosted archive",
        )

        deny_hook_start = len(_hook_rows(root, session_id))
        send_live(
            f"Use the Shell tool to run exactly `touch {deny_path}`, then explain the result briefly",
        )
        deny_pause = _wait_until(pending_pause, timeout=args.timeout, description="hosted Cursor deny request")
        if deny_path.exists():
            raise RuntimeError("Cursor denied command ran before remote permission response")
        answer_pause(deny_pause, "reject")
        _wait_until(
            lambda: next(
                (
                    row
                    for row in _hook_rows(root, session_id)[deny_hook_start:]
                    if row.get("event") == "stop" and row.get("status") in {"completed", "error", "aborted"}
                ),
                None,
            ),
            timeout=args.timeout,
            description="denied Cursor turn completion",
        )
        if deny_path.exists():
            raise RuntimeError("Cursor command ran after remote permission denial")

        hook_start = len(_hook_rows(root, session_id))
        send_live(
            f"Use the Shell tool to run sleep 30, then reply with {forbidden}",
        )
        shell = _wait_until(
            lambda: next(
                (
                    row
                    for row in _hook_rows(root, session_id)[hook_start:]
                    if row.get("event") == "beforeShellExecution" and row.get("command") == "sleep 30"
                ),
                None,
            ),
            timeout=args.timeout,
            description="active Cursor shell generation",
        )
        cancel_pause = _wait_until(pending_pause, timeout=args.timeout, description="hosted Cursor cancel-test permission request")
        answer_pause(cancel_pause, "answer")
        time.sleep(0.5)
        cancel_generation = str(shell.get("generation_id") or "")
        interrupt_live()
        _wait_until(
            lambda: next(
                (
                    row
                    for row in _hook_rows(root, session_id)[hook_start:]
                    if row.get("event") == "stop"
                    and row.get("generation_id") == cancel_generation
                    and row.get("status") in {"aborted", "error"}
                ),
                None,
            ),
            timeout=args.timeout,
            description="cancelled Cursor generation",
        )
        time.sleep(0.5)
        if any(
            row.get("event") == "afterAgentResponse" and row.get("generation_id") == cancel_generation
            for row in _hook_rows(root, session_id)[hook_start:]
        ):
            raise RuntimeError("cancelled Cursor generation still produced an assistant response")
        if session.process.poll() is not None:
            raise RuntimeError("Cursor TUI exited after interrupt")

        send_live(f"Reply with exactly {recovery}")
        recovered = _wait_until(
            lambda: (payload if recovery in _assistant_texts(payload) else None) if (payload := hosted_events()) else None,
            timeout=args.timeout,
            description="post-cancel Cursor recovery in hosted archive",
        )
        recovery_archive_lag = (datetime.now(UTC) - _response_observed_at(_hook_rows(root, session_id), recovery)).total_seconds()
        archive_lags = [first_archive_lag, second_archive_lag, recovery_archive_lag]
        if restart_archive_lag is not None:
            archive_lags.append(restart_archive_lag)
        if max(archive_lags) > args.max_archive_lag:
            raise RuntimeError(
                f"Cursor archive lag exceeded {args.max_archive_lag:.1f}s: " + ", ".join(f"{value:.2f}s" for value in archive_lags)
            )
        report.update(
            {
                "status": "passed",
                "finished_at": _now(),
                "session_id": session_id,
                "provider_conversation_id": claim["conversation_uuid"],
                "cursor_pid": state["cursor_pid"],
                "first_event_count": first["total"],
                "second_event_count": second["total"],
                "recovery_event_count": recovered["total"],
                "cancel_generation_id": cancel_generation,
                "process_alive_after_cancel": True,
                "remote_permission_allow": True,
                "remote_permission_deny": True,
                "machine_agent_restart": machine_agent_restart,
                "archive_lag_seconds": {
                    "first": round(first_archive_lag, 3),
                    "second": round(second_archive_lag, 3),
                    "machine_agent_restart": round(restart_archive_lag, 3) if restart_archive_lag is not None else None,
                    "recovery": round(recovery_archive_lag, 3),
                },
            }
        )
        return report
    except Exception as exc:
        report.update({"status": "failed", "finished_at": _now(), "error": str(exc)})
        raise
    finally:
        if session_id and engine:
            try:
                _engine_command(engine, session_id, "stop")
            except Exception:
                pass
        if session is not None:
            session.close()
        allow_path.unlink(missing_ok=True)
        deny_path.unlink(missing_ok=True)
        (artifact_root / "product-e2e.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("/tmp/longhouse-cursor-product-e2e"))
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-archive-lag", type=float, default=10.0)
    parser.add_argument("--model", default="gpt-5.3-codex-low")
    parser.add_argument("--longhouse-bin", default="longhouse")
    parser.add_argument("--engine-bin", default="longhouse-engine")
    parser.add_argument("--skip-machine-agent-restart", action="store_true")
    args = parser.parse_args()
    try:
        report = run_product_e2e(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
