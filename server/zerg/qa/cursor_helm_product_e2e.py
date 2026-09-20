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
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.services.longhouse_paths import get_managed_local_dir


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


def steer_landed_in_generation(
    rows: list[dict[str, Any]],
    *,
    generation_id: str,
    steered_marker: str,
    done_marker: str,
    later_step_command: str,
) -> dict[str, Any]:
    """Did a steer change the course of the generation it was aimed at?

    A queued follow-up is the shape this must reject: Cursor finishes the
    original generation as asked, then answers the steer text in a new
    generation. Seeing the steered marker somewhere later proves nothing.
    """

    in_generation = [row for row in rows if row.get("generation_id") == generation_id]
    responses = [str(row.get("text") or "") for row in in_generation if row.get("event") == "afterAgentResponse"]
    stopped = any(row.get("event") == "stop" for row in in_generation)
    # Cursor's afterAgentResponse is the semantic commit receipt. Some native
    # steers do not emit a separate stop hook, so waiting for stop alone turns
    # a completed intended generation into a false timeout.
    completed = bool(responses) or stopped
    later_step_ran = any(
        row.get("event") == "beforeShellExecution" and later_step_command in str(row.get("command") or "") for row in in_generation
    )
    steered_here = any(steered_marker in text for text in responses)
    finished_original = any(done_marker in text for text in responses)
    steered_elsewhere = any(
        row.get("event") == "afterAgentResponse"
        and row.get("generation_id") != generation_id
        and steered_marker in str(row.get("text") or "")
        for row in rows
    )
    if not completed:
        failure = "steer_target_generation_never_completed"
    elif steered_here and not later_step_ran and not finished_original:
        failure = None
    elif steered_elsewhere:
        failure = "steer_delivered_as_followup"
    elif later_step_ran or finished_original:
        failure = "steer_did_not_change_course"
    else:
        failure = "steer_marker_missing"
    return {
        "passed": failure is None,
        "failure_code": failure,
        "generation_id": generation_id,
        "steered_in_target_generation": steered_here,
        "steered_in_other_generation": steered_elsewhere,
        "later_step_ran_in_target_generation": later_step_ran,
        "original_task_finished": finished_original,
    }


def _generation_completed(rows: list[dict[str, Any]], generation_id: str) -> bool:
    """Return true once the target generation has a terminal receipt.

    Cursor commits a turn with afterAgentResponse; a separate stop hook is
    optional and may be dropped for a native steer. Keep the generation bind
    strict while accepting either terminal receipt.
    """
    return any(row.get("generation_id") == generation_id and row.get("event") in {"afterAgentResponse", "stop"} for row in rows)


def abort_stopped_generation(
    rows: list[dict[str, Any]],
    *,
    generation_id: str,
    forbidden_marker: str,
    recovery_marker: str | None = None,
) -> dict[str, Any]:
    """Did the interrupt stop the active generation without its reply?

    With ``recovery_marker`` the surviving session must also complete a later
    generation that answers with that marker; stopping a turn by breaking the
    session is not an abort.
    """

    in_generation = [row for row in rows if row.get("generation_id") == generation_id]
    aborted = any(row.get("event") == "stop" and row.get("status") in {"aborted", "error"} for row in in_generation)
    responded = any(row.get("event") == "afterAgentResponse" and forbidden_marker in str(row.get("text") or "") for row in in_generation)
    passed = aborted and not responded
    following_completed = None
    if recovery_marker is not None:
        answered = {
            str(row.get("generation_id"))
            for row in rows
            if row.get("event") == "afterAgentResponse"
            and row.get("generation_id") != generation_id
            and recovery_marker in str(row.get("text") or "")
        }
        following_completed = any(
            row.get("event") == "stop" and row.get("status") == "completed" and str(row.get("generation_id")) in answered for row in rows
        )
        passed = passed and following_completed
    return {
        "passed": passed,
        "generation_stopped_aborted": aborted,
        "forbidden_response_produced": responded,
        "following_turn_completed": following_completed,
    }


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

    def send(self, text: str) -> None:
        os.write(self.master_fd, text.encode())

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


def _accept_workspace_trust_if_prompted(session: _PtyProcess, *, timeout: float) -> bool:
    """Approve Cursor's explicit trust prompt for the disposable canary workspace."""

    deadline = time.monotonic() + min(timeout, 10.0)
    while time.monotonic() < deadline:
        try:
            terminal = session.terminal_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            terminal = ""
        if "Workspace Trust Required" in terminal and "Trust this workspace" in terminal:
            # Cursor renders this as a single-key chooser ("press the key
            # shown"), not a line-oriented prompt. Sending Enter leaves some
            # releases parked on the chooser.
            session.send("a")
            return True
        if session.process.poll() is not None:
            raise RuntimeError(f"Cursor exited before workspace trust was resolved (exit={session.process.returncode})")
        time.sleep(0.1)
    return False


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
    kickstart_timed_out = False
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.longhouse.shipper"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # launchctl can wait after launchd has already replaced the process.
        # The status PID and hosted control reconnect below are the product
        # outcome; keep waiting for them instead of trusting the client lifetime.
        kickstart_timed_out = True
    else:
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
    return {
        "old_pid": old_pid,
        "new_pid": int(current["daemon_pid"]),
        "control_status": "connected",
        "kickstart_timed_out": kickstart_timed_out,
    }


def _can_send_live(payload: dict[str, Any]) -> bool:
    capabilities = payload.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("can_send_input") is True


def _can_send_canonical(state: dict[str, Any] | None) -> bool:
    control = (state or {}).get("control")
    actions = control.get("actions") if isinstance(control, dict) else None
    send_input = actions.get("send_input") if isinstance(actions, dict) else None
    return isinstance(send_input, dict) and send_input.get("state") == "available"


def _activity_state(state: dict[str, Any] | None) -> str | None:
    activity = (state or {}).get("activity")
    return activity.get("state") if isinstance(activity, dict) else None


def _run_lifecycle(state: dict[str, Any] | None) -> str | None:
    run = (state or {}).get("run")
    return run.get("lifecycle") if isinstance(run, dict) else None


def _run_id(state: dict[str, Any] | None) -> str | None:
    run = (state or {}).get("run")
    return run.get("id") if isinstance(run, dict) else None


def _canonical_state_from_diagnostics(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract exact per-session reducer axes from the QA diagnostics surface."""

    if not isinstance(payload, dict) or payload.get("served_path") != "canonical_session_detail":
        return None
    shadow = payload.get("shadow")
    return shadow if isinstance(shadow, dict) else None


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
                getattr(args, "permission_mode", "remote_approve"),
                "--",
                "--model",
                args.model,
                f"Reply with exactly {marker_one}",
            ],
            cwd=workspace,
            terminal_path=terminal_path,
        )
        report["workspace_trust_accepted"] = _accept_workspace_trust_if_prompted(session, timeout=args.timeout)

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
        report["cursor_pid"] = state.get("cursor_pid")
        claim_path = root / "binding-probes" / f"{session_id}.json"
        claim = _wait_until(
            lambda: json.loads(claim_path.read_text()) if claim_path.exists() else None,
            timeout=args.timeout,
            description="native Cursor binding claim",
        )
        url = str(args.api_url or "").rstrip("/")
        token = str(args.agents_token or "")
        if not url or not token:
            raise RuntimeError(
                "Cursor product canary requires --api-url/--agents-token or LONGHOUSE_RUNTIME_API_URL/LONGHOUSE_RUNTIME_AGENTS_TOKEN"
            )
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

        def session_state() -> dict[str, Any] | None:
            payload = api_get(f"/api/agents/sessions/{session_id}/state-diagnostics")
            return _canonical_state_from_diagnostics(payload)

        def settled(deadline: float) -> dict[str, Any]:
            """Served activity must return to quiescent once a turn is done.

            Transcript arrival and the activity axis are independent. Waiting
            only for reply text is what let a finished turn keep reading as
            `Thinking` on every surface: the last shipped activity fact stayed
            `thinking` until its TTL expired, then went to `unknown` instead of
            quiescent. Assert the state the user actually sees, not just that
            the words showed up.
            """

            return _wait_until(
                lambda: (state if _activity_state(state) == "quiescent" else None) if (state := session_state()) else None,
                timeout=deadline,
                description="served Cursor activity settling to quiescent after the turn",
            )

        first = _wait_until(
            lambda: (payload if marker_one in _assistant_texts(payload) else None) if (payload := hosted_events()) else None,
            timeout=args.timeout,
            description="first Cursor reply in hosted archive",
        )
        first_archive_lag = (datetime.now(UTC) - _response_observed_at(_hook_rows(root, session_id), marker_one)).total_seconds()
        first_settled = settled(args.timeout)
        bound_run_id = _run_id(first_settled)
        _wait_until(
            lambda: (state if _can_send_canonical(state) else None) if (state := session_state()) else None,
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
        settled(args.timeout)

        if getattr(args, "lifecycle_only", False):
            lifecycle: dict[str, Any] = {
                "launch_registration": {
                    "state_ready": True,
                    "native_binding_claimed": bool(claim.get("conversation_uuid")),
                    "first_reply_archived": True,
                },
                "send_idle": {"remote_reply_archived": True},
            }
            report["lifecycle"] = lifecycle
            fault = os.environ.get("LH_QA_FAULT") or None
            report["negative_control"] = fault

            # Steer: aim at a generation that is mid-way through three slow
            # sequential shell steps, then prove the steer landed inside it.
            steer_token = uuid4().hex[:10]
            step = f"LONGHOUSE_CURSOR_STEP_{steer_token}"
            steered = f"LONGHOUSE_CURSOR_STEERED_{steer_token}"
            done = f"LONGHOUSE_CURSOR_UNSTEERED_{steer_token}"
            steer_hook_start = len(_hook_rows(root, session_id))
            send_live(
                "Run these three shell commands, each as its own separate Shell tool call, one after another, "
                f"never in parallel: `sleep 6; echo {step}_1`, then `sleep 6; echo {step}_2`, then "
                f"`sleep 6; echo {step}_3`. After all three, reply with exactly {done}"
            )
            first_step = _wait_until(
                lambda: next(
                    (
                        row
                        for row in _hook_rows(root, session_id)[steer_hook_start:]
                        if row.get("event") == "beforeShellExecution" and f"{step}_1" in str(row.get("command") or "")
                    ),
                    None,
                ),
                timeout=args.timeout,
                description="first slow Cursor shell step",
            )
            steer_generation = str(first_step.get("generation_id") or "")
            time.sleep(1.0)
            steer_response = httpx.post(
                f"{url}/api/agents/sessions/{session_id}/input",
                headers=headers,
                json={
                    "text": f"Stop the remaining steps now. Do not run any more commands. Reply with exactly {steered}",
                    "intent": "steer",
                    "client_request_id": uuid.uuid4().hex,
                },
                timeout=30,
            )
            lifecycle["steer_dispatch"] = {"status_code": steer_response.status_code, "body": steer_response.text[:500]}
            if steer_response.is_error:
                raise RuntimeError(f"Runtime Host steer failed HTTP {steer_response.status_code}: {steer_response.text[:1000]}")
            _wait_until(
                lambda: _generation_completed(_hook_rows(root, session_id)[steer_hook_start:], steer_generation),
                timeout=args.timeout,
                description="steered Cursor generation completing",
            )
            # A shell running when the steer lands is backgrounded, and its
            # completion can start one more generation. Let that settle, and let
            # a queued (unsteered) follow-up answer, before judging.
            time.sleep(15.0)
            settled(args.timeout)
            steer_rows = _hook_rows(root, session_id)[steer_hook_start:]
            steer_verdict = steer_landed_in_generation(
                steer_rows,
                generation_id=steer_generation,
                steered_marker=steered,
                done_marker=done,
                later_step_command=f"{step}_3",
            )
            steer_verdict["qa_fault_receipt"] = (
                json.loads(fault_path.read_text()) if (fault_path := root / f"{session_id}.qa-fault.json").exists() else None
            )
            lifecycle["steer_active"] = steer_verdict
            # A steer fault has produced everything its verdict needs; an abort
            # fault has not fired yet, so that run continues through the abort.
            if fault is not None and fault != "cursor_abort_noop":
                report.update({"status": "negative_control_observed", "finished_at": _now()})
                return report
            if not steer_verdict["passed"]:
                raise RuntimeError(f"Cursor steer oracle failed: {steer_verdict}")

            # Abort: cancel an active generation, keep the TUI, and prove the
            # surviving session completes a following turn.
            abort_hook_start = len(_hook_rows(root, session_id))
            send_live(f"Use the Shell tool to run sleep 30, then reply with {forbidden}")
            shell = _wait_until(
                lambda: next(
                    (
                        row
                        for row in _hook_rows(root, session_id)[abort_hook_start:]
                        if row.get("event") == "beforeShellExecution" and row.get("command") == "sleep 30"
                    ),
                    None,
                ),
                timeout=args.timeout,
                description="active Cursor shell generation to abort",
            )
            abort_generation = str(shell.get("generation_id") or "")
            time.sleep(0.5)
            try:
                interrupt_live()
            except RuntimeError as error:
                error_text = str(error)
                failure_code = "abort_generation_rejected" if "generation" in error_text.lower() else "abort_unreached"
                lifecycle["abort_native"] = {"passed": False, "failure_code": failure_code, "interrupt_error": error_text}
                raise
            _wait_until(
                lambda: any(
                    row.get("event") == "stop" and row.get("generation_id") == abort_generation
                    for row in _hook_rows(root, session_id)[abort_hook_start:]
                ),
                timeout=args.timeout,
                description="aborted Cursor generation stop",
            )
            time.sleep(0.5)
            abort_verdict = abort_stopped_generation(
                _hook_rows(root, session_id)[abort_hook_start:], generation_id=abort_generation, forbidden_marker=forbidden
            )
            abort_verdict["tui_alive_after_abort"] = session.process.poll() is None
            if fault == "cursor_abort_noop":
                # A dispatched no-op must leave a receipt for the generation
                # under test. Missing or mismatched evidence distinguishes a
                # genuinely unfired fault from an abort that was never aimed
                # at this generation; neither is a negative-control pass.
                abort_verdict["qa_fault_receipt"] = (
                    json.loads(fault_path.read_text()) if (fault_path := root / f"{session_id}.qa-fault.json").exists() else None
                )
                receipt = abort_verdict["qa_fault_receipt"]
                if receipt is None:
                    abort_verdict.update({"passed": False, "failure_code": "abort_fault_not_fired"})
                    lifecycle["abort_native"] = abort_verdict
                    raise RuntimeError(f"Cursor abort negative control fault receipt missing: {abort_verdict}")
                if str(receipt.get("generation_id") or "") != abort_generation:
                    abort_verdict.update({"passed": False, "failure_code": "abort_generation_changed"})
                    lifecycle["abort_native"] = abort_verdict
                    raise RuntimeError(f"Cursor abort negative control targeted another generation: {abort_verdict}")
                lifecycle["abort_native"] = abort_verdict
                report.update({"status": "negative_control_observed", "finished_at": _now()})
                return report
            settled(args.timeout)
            send_live(f"Reply with exactly {recovery}")
            _wait_until(
                lambda: (payload if recovery in _assistant_texts(payload) else None) if (payload := hosted_events()) else None,
                timeout=args.timeout,
                description="post-abort Cursor turn in hosted archive",
            )
            _wait_until(
                lambda: any(
                    row.get("event") == "stop" and row.get("status") == "completed" and row.get("generation_id") != abort_generation
                    for row in _hook_rows(root, session_id)[abort_hook_start:]
                ),
                timeout=args.timeout,
                description="post-abort Cursor generation completing",
            )
            tui_alive = abort_verdict["tui_alive_after_abort"]
            abort_verdict = abort_stopped_generation(
                _hook_rows(root, session_id)[abort_hook_start:],
                generation_id=abort_generation,
                forbidden_marker=forbidden,
                recovery_marker=recovery,
            )
            abort_verdict["tui_alive_after_abort"] = tui_alive
            abort_verdict["passed"] = bool(abort_verdict["passed"] and tui_alive)
            lifecycle["abort_native"] = abort_verdict
            if not abort_verdict["passed"]:
                raise RuntimeError(f"Cursor abort oracle failed: {abort_verdict}")
            settled(args.timeout)

            # Terminate through the Runtime Host, then prove the owned provider
            # process is gone and the run ended.
            cursor_pid = int(state["cursor_pid"])
            terminate = httpx.post(f"{url}/api/agents/sessions/{session_id}/terminate-live", headers=headers, timeout=30)
            if terminate.is_error or terminate.json().get("terminate_dispatched") is not True:
                raise RuntimeError(f"Runtime Host terminate-live failed HTTP {terminate.status_code}: {terminate.text[:1000]}")
            ended = _wait_until(
                lambda: (current if _run_lifecycle(current) == "ended" else None) if (current := session_state()) else None,
                timeout=args.timeout,
                description="Cursor run reaching ended after remote terminate",
            )

            def provider_gone() -> bool:
                try:
                    os.kill(cursor_pid, 0)
                except ProcessLookupError:
                    return True
                except PermissionError:
                    return False
                return False

            _wait_until(provider_gone, timeout=args.timeout, description="terminated Cursor provider process exit")
            lifecycle["terminate_owned"] = {
                "passed": True,
                "terminate_dispatched": True,
                "run_lifecycle": _run_lifecycle(ended),
                "provider_process_dead": True,
            }
            report.update(
                {
                    "status": "passed",
                    "run_lifecycle_after_teardown": "ended",
                    "activity_after_teardown": _activity_state(ended),
                    "finished_at": _now(),
                    "session_id": session_id,
                    "provider_conversation_id": claim["conversation_uuid"],
                    "cursor_pid": cursor_pid,
                    "qualification_scope": "lifecycle",
                }
            )
            return report

        # Assertion-scoped factory qualification only needs two independent
        # turn boundaries plus clean teardown. Keep the release canary's
        # permission/deny/interrupt/recovery sequence intact by default; it is
        # a separate product contract and must not make the narrower activity
        # assertion depend on unrelated remote-approval behavior.
        if getattr(args, "turn_boundary_only", False):
            archive_lags = [first_archive_lag, second_archive_lag]
            if max(archive_lags) > args.max_archive_lag:
                raise RuntimeError(
                    f"Cursor archive lag exceeded {args.max_archive_lag:.1f}s: " + ", ".join(f"{value:.2f}s" for value in archive_lags)
                )
            _engine_command(engine, session_id, "stop")
            ended = _wait_until(
                lambda: (current if _run_lifecycle(current) == "ended" else None) if (current := session_state()) else None,
                timeout=args.timeout,
                description="Cursor run reaching ended after session teardown",
            )
            if bound_run_id is not None and _run_id(ended) != bound_run_id:
                raise RuntimeError(f"Cursor session changed runs across its life: started on {bound_run_id}, ended on {_run_id(ended)}")
            if _activity_state(ended) == "thinking":
                raise RuntimeError("Cursor session still reports thinking after teardown")
            report.update(
                {
                    "status": "passed",
                    "run_lifecycle_after_teardown": "ended",
                    "activity_after_teardown": _activity_state(ended),
                    "run_id_stable_across_session": True,
                    "finished_at": _now(),
                    "session_id": session_id,
                    "provider_conversation_id": claim["conversation_uuid"],
                    "cursor_pid": state["cursor_pid"],
                    "first_event_count": first["total"],
                    "second_event_count": second["total"],
                    "archive_lag_seconds": {
                        "first": round(first_archive_lag, 3),
                        "second": round(second_archive_lag, 3),
                    },
                    "qualification_scope": "turn_boundary_only",
                }
            )
            return report

        machine_agent_restart = None
        restart_archive_lag = None
        if not args.skip_machine_agent_restart:
            status_path = root.parents[1] / "agent" / "engine-status.json"
            machine_agent_restart = _restart_machine_agent(status_path, timeout=args.timeout)
            if session.process.poll() is not None:
                raise RuntimeError("Cursor TUI exited during Machine Agent restart")
            _wait_until(
                lambda: (state if _can_send_canonical(state) else None) if (state := session_state()) else None,
                timeout=args.timeout,
                description="Cursor live-control lease after Machine Agent restart",
            )
            machine_agent_restart["server_control_status"] = "connected"
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
        settled(args.timeout)
        archive_lags = [first_archive_lag, second_archive_lag, recovery_archive_lag]
        if restart_archive_lag is not None:
            archive_lags.append(restart_archive_lag)
        if max(archive_lags) > args.max_archive_lag:
            raise RuntimeError(
                f"Cursor archive lag exceeded {args.max_archive_lag:.1f}s: " + ", ".join(f"{value:.2f}s" for value in archive_lags)
            )

        # Everything above happens while the session is alive. Teardown is its
        # own failure surface and used to be unobservable here: the canary sent
        # `stop` and wrote its report without ever reading the session again,
        # so a run that outlived the session, or a session left reading
        # `running` forever, passed clean.
        _engine_command(engine, session_id, "stop")
        ended = _wait_until(
            lambda: (state if _run_lifecycle(state) == "ended" else None) if (state := session_state()) else None,
            timeout=args.timeout,
            description="Cursor run reaching ended after session teardown",
        )
        if bound_run_id is not None and _run_id(ended) != bound_run_id:
            raise RuntimeError(f"Cursor session changed runs across its life: started on {bound_run_id}, ended on {_run_id(ended)}")
        if _activity_state(ended) == "thinking":
            raise RuntimeError("Cursor session still reports thinking after teardown")
        settled_state = _activity_state(ended)
        report.update(
            {
                "status": "passed",
                "run_lifecycle_after_teardown": "ended",
                "activity_after_teardown": settled_state,
                "run_id_stable_across_session": True,
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    # The workspace path is what keeps this harness out of the user timeline.
    # `classify_provider_proof_environment` recognises a cwd under
    # `/canaries/provider-live/…/workspace` and normalises the session to
    # environment=test, which default listings exclude.
    #
    # The old default (`/tmp/longhouse-cursor-product-e2e`) matched none of the
    # proof signals, so every run shipped to the real instance as ordinary user
    # work — fourteen rows at the top of a dogfood timeline. A marker in the
    # prompt is not an alternative: classification reads path and machine
    # namespace only, never transcript text.
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.home() / ".longhouse" / "canaries" / "provider-live" / "cursor" / "product-e2e" / "workspace",
    )
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-archive-lag", type=float, default=10.0)
    parser.add_argument("--model", default="gpt-5.3-codex-low")
    parser.add_argument("--longhouse-bin", default="longhouse")
    parser.add_argument("--engine-bin", default="longhouse-engine")
    parser.add_argument("--api-url", default=os.environ.get("LONGHOUSE_RUNTIME_API_URL"))
    parser.add_argument("--agents-token", default=os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"))
    parser.add_argument("--skip-machine-agent-restart", action="store_true")
    parser.add_argument("--turn-boundary-only", action="store_true")
    parser.add_argument("--lifecycle-only", action="store_true")
    parser.add_argument("--permission-mode", default="remote_approve")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        require_disposable_runtime(args.api_url)
        report = run_product_e2e(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
