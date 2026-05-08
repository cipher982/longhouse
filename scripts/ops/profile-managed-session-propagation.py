#!/usr/bin/env python3
"""Profile Codex session propagation from local process truth to timeline truth.

This is the first implementation slice for
docs/specs/managed-session-propagation-profiler.md. It intentionally starts
with Codex because managed Codex has a bridge/control path that can be driven
without solving Claude's native-channel PTY lifecycle first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "managed-session-propagation"
BRIDGE_ROOT = Path.home() / ".claude" / "managed-local" / "codex-bridge"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
HOSTED_CONTAINER_PREFIX = "longhouse-"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def slug_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    timeout: float = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        timeout=timeout,
        text=True,
        capture_output=True,
        check=False,
    )


def safe_json_loads(value: str) -> Any | None:
    try:
        return json.loads(value)
    except Exception:
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


@dataclass
class CommandResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str

    def short(self, limit: int = 4000) -> dict[str, Any]:
        return {
            "cmd": self.cmd,
            "returncode": self.returncode,
            "stdout": self.stdout[-limit:],
            "stderr": self.stderr[-limit:],
        }


class Profiler:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = args.run_id or slug_now()
        self.output_dir = Path(args.output_dir or DEFAULT_OUTPUT_ROOT / self.run_id)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.observations_path = self.output_dir / "observations.jsonl"
        self.summary_path = self.output_dir / "summary.md"
        self.observations: list[dict[str, Any]] = []
        self.started_monotonic_ms = monotonic_ms()
        self.project = args.project
        self.subdomain = args.subdomain
        self.container = args.container or f"{HOSTED_CONTAINER_PREFIX}{self.subdomain}"

    def observe(
        self,
        *,
        case_id: str,
        provider: str,
        ownership: str,
        source: str,
        event: str,
        session_id: str | None = None,
        provider_session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "harness_version": 1,
            "run_id": self.run_id,
            "case_id": case_id,
            "provider": provider,
            "ownership": ownership,
            "session_id": session_id,
            "provider_session_id": provider_session_id,
            "external_correlation_key": payload.get("external_correlation_key") if payload else None,
            "source": source,
            "event": event,
            "observed_at_wall": utc_now(),
            "observed_at_monotonic_ms": monotonic_ms(),
            "clock_skew_ms": None,
            "payload": payload or {},
        }
        self.observations.append(row)
        with self.observations_path.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def run_observed(
        self,
        cmd: list[str],
        *,
        case_id: str,
        ownership: str,
        event_prefix: str,
        timeout: float,
        cwd: Path = ROOT,
        env: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> CommandResult:
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event=f"{event_prefix}_started",
            session_id=session_id,
            payload={"cmd": redact_cmd(cmd)},
        )
        started = monotonic_ms()
        completed = run_cmd(cmd, cwd=cwd, timeout=timeout, env=env)
        result = CommandResult(
            cmd=redact_cmd(cmd),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event=f"{event_prefix}_completed",
            session_id=session_id,
            payload={**result.short(), "duration_ms": monotonic_ms() - started},
        )
        return result

    def local_health(self, session_id: str | None = None) -> dict[str, Any] | None:
        completed = run_cmd(["longhouse", "local-health", "--json"], timeout=30)
        data = safe_json_loads(completed.stdout)
        if not isinstance(data, dict):
            return None
        if session_id is None:
            return data
        managed = [
            item
            for item in data.get("managed_sessions", [])
            if str(item.get("session_id") or item.get("id") or "") == session_id
            or str(item.get("provider_session_id") or "") == session_id
        ]
        unmanaged = [
            item
            for item in data.get("unmanaged_session_bindings", [])
            if str(item.get("session_id") or item.get("id") or "") == session_id
            or str(item.get("provider_session_id") or "") == session_id
        ]
        return {"managed": managed, "unmanaged": unmanaged, "summary": summarize_local_health(data)}

    def hosted_debug(self, session_id: str) -> dict[str, Any] | None:
        cmd = [
            str(ROOT / "scripts" / "ops" / "hosted-session-debug.sh"),
            "--subdomain",
            self.subdomain,
            "--session",
            session_id,
            "--limit",
            "20",
            "--json",
        ]
        completed = run_cmd(cmd, timeout=60)
        data = safe_json_loads(completed.stdout)
        if isinstance(data, dict):
            return data
        return self.hosted_db_direct(session_id)

    def hosted_db_direct(self, session_id: str) -> dict[str, Any] | None:
        script = r"""
import json, sqlite3, sys
subdomain, sid = sys.argv[1], sys.argv[2]
path = f"/var/app-data/longhouse/{subdomain}/longhouse.db"
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
def table(name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
def rows(sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
def one(sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None
payload = {"db_path": path, "session_id": sid}
if table("sessions"):
    payload["session"] = one("SELECT id, provider, project, device_id, cwd, started_at, ended_at, last_activity_at, user_messages, assistant_messages, tool_calls, provider_session_id, summary_title, execution_home, managed_transport, source_runner_name, managed_session_name FROM sessions WHERE id=? OR provider_session_id=?", (sid, sid))
if table("session_runtime_state"):
    payload["runtime_state"] = one("SELECT * FROM session_runtime_state WHERE session_id=? ORDER BY updated_at DESC LIMIT 1", (sid,))
if table("events"):
    payload["event_stats"] = one("SELECT count(*) AS count, min(timestamp) AS first_timestamp, max(timestamp) AS last_timestamp FROM events WHERE session_id=?", (sid,))
    payload["recent_events"] = rows("SELECT id, role, tool_name, substr(coalesce(content_text, tool_output_text, ''), 1, 500) AS text, timestamp FROM events WHERE session_id=? ORDER BY id DESC LIMIT 20", (sid,))
if table("session_runtime_events"):
    payload["runtime_events"] = rows("SELECT id, source, kind, phase, tool_name, occurred_at, received_at, payload_json FROM session_runtime_events WHERE session_id=? ORDER BY id DESC LIMIT 20", (sid,))
print(json.dumps(payload, default=str))
"""
        proc = subprocess.run(
            ["ssh", self.args.ssh_target, "python3", "-", self.subdomain, session_id],
            input=script,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = safe_json_loads(proc.stdout)
        return data if isinstance(data, dict) else None

    def timeline_session(self, session_id: str) -> dict[str, Any] | None:
        script = r"""
import json, sys
from fastapi.testclient import TestClient
from zerg.main import api_app
sid = sys.argv[1]
c = TestClient(api_app)
detail = c.get(f"/timeline/sessions/{sid}")
listing = c.get("/timeline/sessions", params={"project":"zerg","provider":"codex","limit":20})
payload = {"detail_status": detail.status_code, "listing_status": listing.status_code}
if detail.status_code == 200:
    payload["detail"] = detail.json()
if listing.status_code == 200:
    data = listing.json()
    payload["listing_total"] = data.get("total")
    matches = []
    for card in data.get("sessions", []):
        ids = {card.get("thread_id")}
        for key in ("head", "detail", "root"):
            if isinstance(card.get(key), dict):
                ids.add(card[key].get("id"))
        if sid in ids:
            matches.append(card)
    payload["matches"] = matches
print(json.dumps(payload, default=str))
"""
        proc = subprocess.run(
            [
                "ssh",
                self.args.ssh_target,
                "docker",
                "exec",
                "-i",
                "-e",
                "AUTH_DISABLED=1",
                self.container,
                "python3",
                "-",
                session_id,
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        # Container startup logs can precede JSON. Parse the last JSON object line.
        for line in reversed((proc.stdout or "").splitlines()):
            data = safe_json_loads(line)
            if isinstance(data, dict):
                return data
        return None

    def timeline_sse_initial_replay(self, session_id: str) -> dict[str, Any] | None:
        script = r"""
import json, sys
from fastapi.testclient import TestClient
from zerg.main import api_app
sid = sys.argv[1]
c = TestClient(api_app)
seen = []
with c.stream("GET", "/timeline/sessions/stream", params={"project":"zerg","provider":"codex","limit":20}) as r:
    for raw in r.iter_lines():
        if not raw:
            continue
        line = raw.decode() if isinstance(raw, bytes) else raw
        seen.append(line)
        if sid in line or len(seen) > 80:
            break
payload = {"line_count": len(seen), "contains_session": any(sid in line for line in seen), "sample": seen[:20]}
print(json.dumps(payload))
"""
        proc = subprocess.run(
            [
                "ssh",
                self.args.ssh_target,
                "docker",
                "exec",
                "-i",
                "-e",
                "AUTH_DISABLED=1",
                self.container,
                "python3",
                "-",
                session_id,
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        for line in reversed((proc.stdout or "").splitlines()):
            data = safe_json_loads(line)
            if isinstance(data, dict):
                return data
        return {"error": (proc.stderr or proc.stdout or "").strip()[-1000:]}

    def poll_hosted_session(
        self,
        session_id: str,
        *,
        case_id: str,
        ownership: str,
        predicate,
        event: str,
        timeout: float = 180,
        interval: float = 5,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.hosted_db_direct(session_id)
            if last is not None and predicate(last):
                self.observe(
                    case_id=case_id,
                    provider="codex",
                    ownership=ownership,
                    source="hosted_db",
                    event=event,
                    session_id=session_id,
                    payload=compact_hosted(last),
                )
                return last
            time.sleep(interval)
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="hosted_db",
            event=f"{event}_timeout",
            session_id=session_id,
            payload=compact_hosted(last or {}),
        )
        return last

    def run_managed_codex(self) -> dict[str, Any]:
        case_id = "B1"
        ownership = "managed"
        nonce = f"LH_PROBE_CODEX_MANAGED_{self.run_id}"
        name = f"{self.args.name_prefix}-managed-{self.run_id}"
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event="launch_requested",
            payload={"nonce": nonce, "name": name},
        )
        launch = self.run_observed(
            [
                "longhouse",
                "codex",
                "--cwd",
                str(ROOT),
                "--project",
                self.project,
                "--name",
                name,
                "--no-attach",
                "--no-open",
            ],
            case_id=case_id,
            ownership=ownership,
            event_prefix="managed_launch",
            timeout=90,
        )
        session_id = parse_session_id(launch.stdout)
        ws_url = parse_remote_target(launch.stdout)
        if not session_id or not ws_url:
            raise RuntimeError(f"managed launch did not return session/ws url: {launch.short()}")
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event="session_id_observed",
            session_id=session_id,
            provider_session_id=session_id,
            payload={"ws_url": ws_url},
        )
        self.write_snapshot(case_id, ownership, session_id, "post_launch")

        tui_log = self.output_dir / f"{session_id}-managed-tui.log"
        remote_exec = f"exec {shlex.quote('/opt/homebrew/bin/codex')} --enable tui_app_server --remote {shlex.quote(ws_url)} --no-alt-screen"
        remote_cmd = (
            "stty rows 40 cols 120 2>/dev/null || true; "
            "export LINES=40 COLUMNS=120 TERM=${TERM:-xterm-256color}; "
            f"{remote_exec}"
        )
        tui = subprocess.Popen(
            ["script", "-q", str(tui_log), "zsh", "-lc", remote_cmd],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event="remote_tui_started",
            session_id=session_id,
            payload={"pid": tui.pid, "log": str(tui_log)},
        )
        state = self.wait_bridge_thread(session_id, case_id=case_id, ownership=ownership)
        thread_id = state.get("thread_id") if state else None
        thread_path = Path(str(state.get("thread_path") or "")) if state else None
        if not thread_id:
            raise RuntimeError("remote TUI did not create a managed Codex thread")
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="codex_bridge_state",
            event="managed_state_observed",
            session_id=session_id,
            payload={"thread_id": thread_id, "state": state},
        )

        send = self.run_observed(
            [
                "longhouse-engine",
                "codex-bridge",
                "send",
                "--session-id",
                session_id,
                "--text",
                f"Reply with exactly {nonce}",
                "--json",
            ],
            case_id=case_id,
            ownership=ownership,
            event_prefix="prompt_sent",
            timeout=240,
            session_id=session_id,
        )
        if send.returncode != 0:
            raise RuntimeError(f"managed send failed: {send.short()}")
        if thread_path:
            self.poll_local_assistant_response(
                thread_path,
                nonce,
                case_id=case_id,
                ownership=ownership,
                session_id=session_id,
            )
        self.poll_hosted_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=lambda data: hosted_assistant_events_contain(data, nonce),
            event="assistant_response_hosted",
            timeout=180,
            interval=0.5,
        )
        self.write_snapshot(case_id, ownership, session_id, "post_response")

        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event="shutdown_requested",
            session_id=session_id,
        )
        self.run_observed(
            ["longhouse-engine", "codex-bridge", "stop", "--session-id", session_id],
            case_id=case_id,
            ownership=ownership,
            event_prefix="shutdown",
            timeout=60,
            session_id=session_id,
        )
        terminate_process(tui)
        self.poll_hosted_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=lambda data: lifecycle_closed(data),
            event="hosted_runtime_closed",
            timeout=15,
            interval=0.25,
        )
        self.write_snapshot(case_id, ownership, session_id, "post_shutdown")
        return {
            "case_id": case_id,
            "session_id": session_id,
            "nonce": nonce,
            "thread_id": thread_id,
            "thread_path": str(thread_path) if thread_path else None,
        }

    def wait_bridge_thread(self, session_id: str, *, case_id: str, ownership: str) -> dict[str, Any] | None:
        state_path = BRIDGE_ROOT / f"{session_id}.json"
        deadline = time.monotonic() + 60
        last = None
        while time.monotonic() < deadline:
            last = read_json(state_path)
            if (
                last
                and str(last.get("thread_id") or "").strip()
                and str(last.get("thread_path") or "").strip()
            ):
                return last
            time.sleep(1)
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="codex_bridge_state",
            event="managed_state_timeout",
            session_id=session_id,
            payload={"state_path": str(state_path), "last": last},
        )
        return last

    def poll_local_assistant_response(
        self,
        path: Path,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
        session_id: str,
        timeout: float = 180,
        interval: float = 0.1,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        last_size = None
        while time.monotonic() < deadline:
            event = find_local_assistant_event(path, nonce)
            if event is not None:
                self.observe(
                    case_id=case_id,
                    provider="codex",
                    ownership=ownership,
                    source="provider_transcript",
                    event="assistant_response_local",
                    session_id=session_id,
                    payload={"path": str(path), **event},
                )
                return event
            try:
                last_size = path.stat().st_size
            except OSError:
                last_size = None
            time.sleep(interval)
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="provider_transcript",
            event="assistant_response_local_timeout",
            session_id=session_id,
            payload={"path": str(path), "last_size": last_size},
        )
        return None

    def run_unmanaged_codex(self) -> dict[str, Any]:
        case_id = "A1"
        ownership = "unmanaged"
        nonce = f"LH_PROBE_CODEX_UNMANAGED_{self.run_id}"
        before = time.time()
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event="launch_requested",
            payload={"nonce": nonce},
        )
        result = self.run_observed(
            [
                "codex",
                "exec",
                "--cd",
                str(ROOT),
                "--sandbox",
                "read-only",
                "-c",
                "model_reasoning_effort=low",
                f"Reply with exactly {nonce}",
            ],
            case_id=case_id,
            ownership=ownership,
            event_prefix="unmanaged_exec",
            timeout=240,
        )
        if result.returncode != 0:
            raise RuntimeError(f"unmanaged codex exec failed: {result.short()}")
        rollout = find_rollout_with_nonce(nonce, since_epoch=before)
        if not rollout:
            raise RuntimeError(f"could not find Codex rollout containing nonce {nonce}")
        session_id = parse_session_id_from_rollout(rollout)
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="provider_transcript",
            event="session_id_observed",
            session_id=session_id,
            provider_session_id=session_id,
            payload={"path": str(rollout), "external_correlation_key": str(rollout)},
        )
        self.poll_hosted_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=lambda data: bool(data.get("session")),
            event="timeline_card_observed",
            timeout=180,
        )
        self.poll_hosted_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=lambda data: hosted_assistant_events_contain(data, nonce),
            event="assistant_response_hosted",
            timeout=180,
            interval=0.5,
        )
        self.poll_hosted_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=lambda data: lifecycle_closed(data),
            event="hosted_runtime_closed",
            timeout=180,
            interval=0.25,
        )
        self.write_snapshot(case_id, ownership, session_id, "post_exec")
        return {"case_id": case_id, "session_id": session_id, "nonce": nonce, "rollout": str(rollout)}

    def write_snapshot(self, case_id: str, ownership: str, session_id: str, label: str) -> None:
        local = call_or_error(lambda: self.local_health(session_id))
        hosted = call_or_error(lambda: self.hosted_debug(session_id))
        timeline = call_or_error(lambda: self.timeline_session(session_id))
        sse = call_or_error(lambda: self.timeline_sse_initial_replay(session_id))
        payload = {"local_health": local, "hosted_debug": compact_hosted(hosted or {}), "timeline": compact_timeline(timeline or {}), "sse": sse}
        path = self.output_dir / f"{case_id}-{label}-{session_id}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="harness",
            event=f"snapshot_{label}",
            session_id=session_id,
            payload={"path": str(path), **compact_snapshot(payload)},
        )

    def write_summary(self, results: list[dict[str, Any]], errors: list[str]) -> None:
        lines = [
            "# Managed Session Propagation Profile",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Started: `{utc_now()}`",
            f"- Project: `{self.project}`",
            f"- Subdomain: `{self.subdomain}`",
            f"- Observations: `{self.observations_path}`",
            "",
            "## Results",
            "",
            "| Case | Session | Nonce | Verdict | Notes |",
            "| --- | --- | --- | --- | --- |",
        ]
        for result in results:
            case_id = result.get("case_id", "-")
            sid = result.get("session_id", "-")
            nonce = result.get("nonce", "-")
            verdict, notes = self.verdict_for(case_id, sid, nonce)
            lines.append(f"| {case_id} | `{sid}` | `{nonce}` | {verdict} | {notes} |")
        if errors:
            lines.extend(["", "## Errors", ""])
            lines.extend(f"- {err}" for err in errors)
        lines.extend(["", "## Artifact Directory", "", f"`{self.output_dir}`", ""])
        self.summary_path.write_text("\n".join(lines))

    def verdict_for(self, case_id: str, session_id: str, nonce: str) -> tuple[str, str]:
        hosted = self.hosted_db_direct(session_id) or {}
        session = hosted.get("session") or {}
        runtime = hosted.get("runtime_state") or {}
        contains = hosted_assistant_events_contain(hosted, nonce)
        closed = lifecycle_closed(hosted)
        transcript_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "assistant_response_hosted",
        )
        provider_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "assistant_response_local",
        )
        propagation_latency = self.event_delta_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "assistant_response_hosted",
        )
        close_latency = self.event_delta_ms(case_id, session_id, "shutdown_requested", "hosted_runtime_closed")
        terminal = terminal_details(hosted)
        ownership = session.get("execution_home") or "-"
        transport = session.get("managed_transport") or "-"
        if not session:
            return "missing", "hosted session row not observed"
        transcript = "synced" if contains else "missing"
        if transcript_latency is not None:
            transcript += f" observed_in={transcript_latency}ms"
        if provider_latency is not None:
            transcript += f" provider={provider_latency}ms"
        if propagation_latency is not None:
            transcript += f" local_to_hosted={propagation_latency}ms"
        close_note = "close=missing"
        if closed:
            close_note = "close=closed"
            if close_latency is not None:
                close_note += f" observed_in={close_latency}ms"
            if terminal.get("ingest_lag_ms") is not None:
                close_note += f" ingest_lag={terminal['ingest_lag_ms']}ms"
            if terminal.get("source"):
                close_note += f" source={terminal['source']}"
            if terminal.get("reason"):
                close_note += f" reason={terminal['reason']}"
        if not contains:
            verdict = "partial" if closed else "missing"
            return verdict, f"transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
        if not closed:
            phase = runtime.get("phase") or runtime.get("terminal_state") or "-"
            return "pass", f"nonce synced; close not confirmed yet; phase={phase}; ownership={ownership}, transport={transport}"
        return "pass", f"transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"

    def event_delta_ms(self, case_id: str, session_id: str, start_event: str, end_event: str) -> int | None:
        start = None
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") == start_event and start is None:
                start = row.get("observed_at_monotonic_ms")
            if row.get("event") == end_event and start is not None:
                end = row.get("observed_at_monotonic_ms")
                if isinstance(start, int) and isinstance(end, int):
                    return end - start
        return None


def redact_cmd(cmd: list[str]) -> list[str]:
    redacted = []
    skip_value = False
    for part in cmd:
        if skip_value:
            redacted.append("<redacted>")
            skip_value = False
            continue
        if part in {"--token", "-t"}:
            redacted.append(part)
            skip_value = True
        else:
            redacted.append(part)
    return redacted


def parse_session_id(text: str) -> str | None:
    match = re.search(r"Session ID:\s*([0-9a-fA-F-]{36})", text)
    return match.group(1) if match else None


def parse_remote_target(text: str) -> str | None:
    match = re.search(r"Remote target:\s*(ws://\S+|wss://\S+)", text)
    return match.group(1) if match else None


def parse_session_id_from_rollout(path: Path) -> str:
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$",
        path.name,
    )
    if not match:
        raise ValueError(f"could not parse session id from {path}")
    return match.group(1)


def find_rollout_with_nonce(nonce: str, *, since_epoch: float) -> Path | None:
    if not CODEX_SESSIONS_ROOT.exists():
        return None
    candidates = []
    for path in CODEX_SESSIONS_ROOT.rglob("rollout-*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime + 5 < since_epoch:
            continue
        candidates.append((stat.st_mtime, path))
    for _mtime, path in sorted(candidates, reverse=True):
        try:
            if nonce in path.read_text(errors="ignore"):
                return path
        except OSError:
            continue
    return None


def find_local_assistant_event(path: Path, nonce: str) -> dict[str, Any] | None:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    for line_number, line in reversed(list(enumerate(lines, start=1))):
        if nonce not in line:
            continue
        data = safe_json_loads(line)
        if not isinstance(data, dict):
            continue
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        timestamp = data.get("timestamp")
        if is_codex_assistant_payload(payload, nonce):
            return {
                "line_number": line_number,
                "timestamp": timestamp,
                "type": data.get("type"),
                "payload_type": payload.get("type"),
            }
    return None


def is_codex_assistant_payload(payload: dict[str, Any], nonce: str) -> bool:
    payload_type = str(payload.get("type") or "")
    if payload_type == "agent_message":
        return nonce in str(payload.get("message") or "")
    if payload_type != "message" or str(payload.get("role") or "") != "assistant":
        return False
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        if nonce in str(item.get("text") or ""):
            return True
    return False


def hosted_assistant_events_contain(data: dict[str, Any], text: str) -> bool:
    for event in data.get("recent_events") or []:
        if str(event.get("role") or "") != "assistant":
            continue
        if text in str(event.get("text") or ""):
            return True
    return False


def lifecycle_closed(data: dict[str, Any]) -> bool:
    runtime = data.get("runtime_state") or {}
    terminal = str(runtime.get("terminal_state") or "").strip().lower()
    if terminal:
        return True
    for event in data.get("runtime_events") or []:
        payload = str(event.get("payload_json") or "")
        if "process_gone" in payload:
            return True
    session = data.get("session") or {}
    return bool(session.get("ended_at"))


def terminal_details(data: dict[str, Any]) -> dict[str, Any]:
    runtime = data.get("runtime_state") or {}
    details = {
        "state": str(runtime.get("terminal_state") or "").strip() or None,
        "reason": str(runtime.get("terminal_reason") or "").strip() or None,
        "source": str(runtime.get("terminal_source") or "").strip() or None,
        "ingest_lag_ms": None,
    }
    for event in data.get("runtime_events") or []:
        if event.get("kind") != "terminal_signal":
            continue
        details["source"] = details["source"] or str(event.get("source") or "").strip() or None
        occurred_at = parse_db_timestamp(event.get("occurred_at"))
        received_at = parse_db_timestamp(event.get("received_at"))
        if occurred_at is not None and received_at is not None:
            details["ingest_lag_ms"] = int((received_at - occurred_at).total_seconds() * 1000)
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        if isinstance(payload, dict):
            details["state"] = details["state"] or str(payload.get("terminal_state") or "").strip() or None
            details["reason"] = details["reason"] or str(payload.get("terminal_reason") or "").strip() or None
            details["source"] = details["source"] or str(payload.get("terminal_source") or "").strip() or None
        break
    return details


def parse_db_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def compact_hosted(data: dict[str, Any]) -> dict[str, Any]:
    if not data:
        return {}
    return {
        "session": data.get("session"),
        "runtime_state": data.get("runtime_state"),
        "event_stats": data.get("event_stats"),
        "recent_events": (data.get("recent_events") or [])[:5],
        "runtime_events": (data.get("runtime_events") or [])[:5],
    }


def compact_timeline(data: dict[str, Any]) -> dict[str, Any]:
    detail = data.get("detail") or {}
    matches = data.get("matches") or []
    return {
        "detail_status": data.get("detail_status"),
        "listing_status": data.get("listing_status"),
        "listing_total": data.get("listing_total"),
        "detail": {
            key: detail.get(key)
            for key in [
                "id",
                "summary_title",
                "execution_home",
                "managed_transport",
                "status",
                "display_phase",
                "runtime_display",
                "timeline_card",
                "capabilities",
            ]
        },
        "matches": [
            {
                "thread_id": card.get("thread_id"),
                "timeline_anchor_at": card.get("timeline_anchor_at"),
                "head": {
                    "id": (card.get("head") or {}).get("id"),
                    "summary_title": (card.get("head") or {}).get("summary_title"),
                    "timeline_card": (card.get("head") or {}).get("timeline_card"),
                    "runtime_display": (card.get("head") or {}).get("runtime_display"),
                },
            }
            for card in matches[:3]
        ],
    }


def compact_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "local_health": payload.get("local_health"),
        "hosted_debug": payload.get("hosted_debug"),
        "timeline": payload.get("timeline"),
        "sse": payload.get("sse"),
    }


def summarize_local_health(data: dict[str, Any]) -> dict[str, Any]:
    launch = data.get("launch_readiness") or {}
    return {
        "health_state": data.get("health_state"),
        "managed_count": len(data.get("managed_sessions") or []),
        "unmanaged_count": len(data.get("unmanaged_session_bindings") or []),
        "control_plane_url": launch.get("control_plane_url"),
        "machine_name": launch.get("machine_name"),
    }


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def call_or_error(fn):
    try:
        return fn()
    except subprocess.TimeoutExpired as exc:
        return {"error": f"timeout after {exc.timeout}s", "cmd": redact_cmd(list(exc.cmd)) if isinstance(exc.cmd, list) else str(exc.cmd)}
    except Exception as exc:
        return {"error": str(exc)}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["codex"], default="codex")
    parser.add_argument("--ownership", choices=["managed", "unmanaged", "all"], default="all")
    parser.add_argument("--subdomain", default="david010")
    parser.add_argument("--container")
    parser.add_argument("--ssh-target", default="zerg")
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--name-prefix", default="lh-probe")
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir")
    parser.add_argument("--skip-managed", action="store_true")
    parser.add_argument("--skip-unmanaged", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    profiler = Profiler(args)
    profiler.observe(
        case_id="run",
        provider="codex",
        ownership=args.ownership,
        source="harness",
        event="run_started",
        payload={
            "output_dir": str(profiler.output_dir),
            "project": args.project,
            "subdomain": args.subdomain,
            "container": profiler.container,
        },
    )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        if args.ownership in {"managed", "all"} and not args.skip_managed:
            results.append(profiler.run_managed_codex())
    except Exception as exc:
        errors.append(f"managed Codex failed: {exc}")
        profiler.observe(
            case_id="B1",
            provider="codex",
            ownership="managed",
            source="harness",
            event="mismatch_detected",
            payload={"error": str(exc)},
        )
    try:
        if args.ownership in {"unmanaged", "all"} and not args.skip_unmanaged:
            results.append(profiler.run_unmanaged_codex())
    except Exception as exc:
        errors.append(f"unmanaged Codex failed: {exc}")
        profiler.observe(
            case_id="A1",
            provider="codex",
            ownership="unmanaged",
            source="harness",
            event="mismatch_detected",
            payload={"error": str(exc)},
        )
    profiler.write_summary(results, errors)
    print(profiler.summary_path)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
