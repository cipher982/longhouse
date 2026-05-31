#!/usr/bin/env python3
"""Probe managed Claude truth across local and hosted Longhouse surfaces.

This is an exploratory probe, not an SLA gate. It exists to learn which
Claude-specific signal should become the primary clock for managed Claude
profiles.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "managed-claude-truth"
SENSITIVE_KEY_PARTS = ("token", "secret", "auth", "password", "cookie", "authorization")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("<redacted>" if is_sensitive_key(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def strip_sensitive_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): strip_sensitive_keys(item) for key, item in value.items() if not is_sensitive_key(str(key))}
    if isinstance(value, list):
        return [strip_sensitive_keys(item) for item in value]
    return value


def pid_alive(pid: Any) -> bool:
    try:
        normalized = int(pid)
    except (TypeError, ValueError):
        return False
    if normalized <= 0:
        return False
    try:
        os.kill(normalized, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_cmd(cmd: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        timeout=timeout,
        text=True,
        capture_output=True,
        check=False,
    )


def command_available(name: str) -> bool:
    return run_cmd(["/usr/bin/env", "sh", "-lc", f"command -v {name} >/dev/null 2>&1"], timeout=5).returncode == 0


def state_file_for(session_id: str, *, claude_dir: Path) -> Path:
    return claude_dir / "channels" / "longhouse" / "sessions" / f"{session_id}.json"


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return strip_sensitive_keys(raw) if isinstance(raw, dict) else None


def file_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def http_json(url: str, *, timeout: float = 3) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    data = safe_json_loads(raw)
    return data if isinstance(data, dict) else None


def ps_row(pid: Any) -> dict[str, Any] | None:
    try:
        normalized = int(pid)
    except (TypeError, ValueError):
        return None
    if normalized <= 0:
        return None
    proc = run_cmd(["ps", "-p", str(normalized), "-o", "pid=,ppid=,stat=,command="], timeout=5)
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip()
    if not line:
        return None
    parts = line.split(None, 3)
    return {
        "pid": int(parts[0]) if parts else normalized,
        "ppid": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
        "stat": parts[2] if len(parts) > 2 else None,
        "command": parts[3] if len(parts) > 3 else line,
    }


def collect_local_health(*, fast: bool) -> dict[str, Any] | None:
    cmd = ["longhouse", "local-health", "--json"]
    if fast:
        cmd.append("--fast")
    proc = run_cmd(cmd, timeout=30)
    data = safe_json_loads(proc.stdout or "")
    if isinstance(data, dict):
        return data
    return {
        "error": "local_health_unparseable",
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-1000:],
        "stdout": (proc.stdout or "")[-1000:],
    }


def managed_claude_rows(local_health: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(local_health, dict):
        return []
    rows = local_health.get("managed_sessions")
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("provider") or "").strip() == "claude"
        and str(row.get("control_path") or "").strip() == "managed"
    ]


def select_session_id(local_health: dict[str, Any] | None, requested: str | None) -> str | None:
    if requested:
        return requested
    rows = managed_claude_rows(local_health)
    if not rows:
        return None
    rows.sort(key=lambda row: str(row.get("last_activity_at") or row.get("started_at") or ""), reverse=True)
    return str(rows[0].get("session_id") or "").strip() or None


def filter_local_health(local_health: dict[str, Any] | None, session_id: str) -> dict[str, Any] | None:
    if not isinstance(local_health, dict):
        return None
    managed = [
        row
        for row in local_health.get("managed_sessions") or []
        if isinstance(row, dict)
        and (
            str(row.get("session_id") or "") == session_id
            or str(row.get("provider_session_id") or "") == session_id
        )
    ]
    engine_payload = (local_health.get("engine_status") or {}).get("payload") or {}
    phase_ledger = [
        row
        for row in engine_payload.get("phase_ledger") or []
        if isinstance(row, dict)
        and (
            str(row.get("session_id") or "") == session_id
            or str(row.get("provider_session_id") or "") == session_id
        )
    ]
    return {
        "health_state": local_health.get("health_state"),
        "severity": local_health.get("severity"),
        "headline": local_health.get("headline"),
        "managed_summary": local_health.get("managed_summary"),
        "transport_health": local_health.get("transport_health"),
        "engine_status": {
            "exists": (local_health.get("engine_status") or {}).get("exists"),
            "age_seconds": (local_health.get("engine_status") or {}).get("age_seconds"),
            "path": (local_health.get("engine_status") or {}).get("path"),
        },
        "managed": managed,
        "phase_ledger": phase_ledger,
    }


def provider_session_id_from_local_health(local_health: dict[str, Any] | None, session_id: str | None) -> str | None:
    if not session_id or not isinstance(local_health, dict):
        return None
    for row in local_health.get("managed_sessions") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("session_id") or "") == session_id:
            return str(row.get("provider_session_id") or "").strip() or None
    return None


def collect_hook_outbox(*, longhouse_home: Path, session_id: str, limit: int = 20) -> dict[str, Any]:
    outbox_dir = longhouse_home / "agent" / "outbox"
    if not outbox_dir.exists():
        return {"path": str(outbox_dir), "exists": False, "entries": []}
    entries: list[dict[str, Any]] = []
    for path in sorted(outbox_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        payload = read_json_file(path)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("session_id") or "") != session_id:
            continue
        entries.append(
            {
                "path": str(path),
                "mtime": file_mtime_iso(path),
                "payload": payload,
            }
        )
    entries.sort(key=lambda entry: str(entry.get("mtime") or ""), reverse=True)
    return {
        "path": str(outbox_dir),
        "exists": True,
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


def hosted_debug(session_id: str, *, subdomain: str, limit: int) -> dict[str, Any] | None:
    script = ROOT / "scripts" / "ops" / "hosted-session-debug.sh"
    if not script.exists():
        return {"error": f"missing {script}"}
    proc = run_cmd(
        [
            str(script),
            "--subdomain",
            subdomain,
            "--session",
            session_id,
            "--limit",
            str(limit),
            "--json",
        ],
        timeout=60,
    )
    data = safe_json_loads(proc.stdout or "")
    if isinstance(data, dict):
        return data
    return {
        "error": "hosted_debug_unparseable",
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-1000:],
        "stdout": (proc.stdout or "")[-1000:],
    }


@dataclass
class Recorder:
    path: Path
    run_id: str
    case_id: str
    profile_class: str

    def write(
        self,
        *,
        source: str,
        event: str,
        session_id: str | None = None,
        provider_session_id: str | None = None,
        external_correlation_key: str | None = None,
        clock_skew_ms: int | None = None,
        payload: Any = None,
    ) -> None:
        row = {
            "schema": "managed_claude_truth_probe.v1",
            "harness_version": 1,
            "run_id": self.run_id,
            "profile_class": self.profile_class,
            "case_id": self.case_id,
            "provider": "claude",
            "ownership": "managed",
            "observed_at_wall": utc_now(),
            "observed_at_monotonic_ms": int(time.monotonic() * 1000),
            "source": source,
            "event": event,
            "session_id": session_id,
            "provider_session_id": provider_session_id,
            "external_correlation_key": external_correlation_key or provider_session_id or session_id,
            "clock_skew_ms": clock_skew_ms,
            "payload": redact(payload if payload is not None else {}),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def summarize_probe(
    *,
    session_id: str | None,
    local_health: dict[str, Any] | None,
    channel_state: dict[str, Any] | None,
    channel_health: dict[str, Any] | None,
    hook_outbox: dict[str, Any] | None,
    hosted: dict[str, Any] | None,
) -> dict[str, Any]:
    local_rows = []
    phase_ledger_rows = []
    if session_id and isinstance(local_health, dict):
        filtered = filter_local_health(local_health, session_id)
        local_rows = (filtered or {}).get("managed") or []
        phase_ledger_rows = (filtered or {}).get("phase_ledger") or []
    hosted_database = hosted.get("database") if isinstance(hosted, dict) else None
    hosted_source = hosted_database if isinstance(hosted_database, dict) else hosted
    runtime_state = hosted_source.get("runtime_state") if isinstance(hosted_source, dict) else None
    session = hosted_source.get("session") if isinstance(hosted_source, dict) else None
    event_stats = hosted_source.get("event_stats") if isinstance(hosted_source, dict) else None
    runtime_event_stats = hosted_source.get("runtime_event_stats") if isinstance(hosted_source, dict) else None
    runtime_events = hosted_source.get("recent_runtime_events") if isinstance(hosted_source, dict) else []
    terminal_events = [
        event
        for event in (runtime_events or [])
        if isinstance(event, dict) and str(event.get("kind") or "").strip() == "terminal_signal"
    ]
    log_counts = hosted.get("log_counts") if isinstance(hosted, dict) else None
    write_serializer = (log_counts or {}).get("write_serializer") if isinstance(log_counts, dict) else None
    hook_entries = hook_outbox.get("entries") if isinstance(hook_outbox, dict) else []
    latest_hook = hook_entries[0] if hook_entries else {}
    latest_hook_payload = latest_hook.get("payload") if isinstance(latest_hook, dict) else {}
    latest_phase_ledger = phase_ledger_rows[0] if phase_ledger_rows else {}
    return {
        "session_id": session_id,
        "local_health_has_managed_claude": bool(local_rows),
        "local_health_state": local_rows[0].get("state") if local_rows else None,
        "local_health_phase": local_rows[0].get("raw_phase") if local_rows else None,
        "local_phase_ledger_entries": len(phase_ledger_rows),
        "latest_phase_ledger_phase": latest_phase_ledger.get("phase") if isinstance(latest_phase_ledger, dict) else None,
        "latest_phase_ledger_source": latest_phase_ledger.get("source") if isinstance(latest_phase_ledger, dict) else None,
        "latest_phase_ledger_observed_at": latest_phase_ledger.get("observed_at") if isinstance(latest_phase_ledger, dict) else None,
        "channel_state_exists": channel_state is not None,
        "channel_ready": bool((channel_state or {}).get("ready")),
        "channel_health_available": channel_health is not None,
        "claude_pid_alive": pid_alive((channel_state or {}).get("claude_pid")),
        "bridge_pid_alive": pid_alive((channel_state or {}).get("bridge_pid")),
        "hook_outbox_entries": len(hook_entries),
        "latest_hook_state": latest_hook_payload.get("state") if isinstance(latest_hook_payload, dict) else None,
        "latest_hook_mtime": latest_hook.get("mtime") if isinstance(latest_hook, dict) else None,
        "hosted_session_exists": bool(session),
        "hosted_runtime_phase": (runtime_state or {}).get("phase") if isinstance(runtime_state, dict) else None,
        "hosted_phase_source": (runtime_state or {}).get("phase_source") if isinstance(runtime_state, dict) else None,
        "hosted_terminal_state": (runtime_state or {}).get("terminal_state") if isinstance(runtime_state, dict) else None,
        "hosted_terminal_reason": (runtime_state or {}).get("terminal_reason") if isinstance(runtime_state, dict) else None,
        "hosted_terminal_source": (runtime_state or {}).get("terminal_source") if isinstance(runtime_state, dict) else None,
        "hosted_runtime_updated_at": (runtime_state or {}).get("updated_at") if isinstance(runtime_state, dict) else None,
        "hosted_archive_event_count": (event_stats or {}).get("count") if isinstance(event_stats, dict) else None,
        "hosted_archive_assistant_events": (event_stats or {}).get("assistant_events") if isinstance(event_stats, dict) else None,
        "hosted_archive_tool_events": (event_stats or {}).get("tool_events") if isinstance(event_stats, dict) else None,
        "hosted_session_user_messages": (session or {}).get("user_messages") if isinstance(session, dict) else None,
        "hosted_session_assistant_messages": (session or {}).get("assistant_messages") if isinstance(session, dict) else None,
        "hosted_transcript_revision": (session or {}).get("transcript_revision") if isinstance(session, dict) else None,
        "hosted_runtime_event_count": (runtime_event_stats or {}).get("count")
        if isinstance(runtime_event_stats, dict)
        else None,
        "hosted_terminal_event_count": len(terminal_events),
        "hosted_terminal_event_sources": [
            str(event.get("source") or "").strip() for event in terminal_events if str(event.get("source") or "").strip()
        ],
        "hosted_write_serializer_avg_wait_ms": (write_serializer or {}).get("avg_wait_ms")
        if isinstance(write_serializer, dict)
        else None,
        "hosted_write_serializer_max_wait_ms": (write_serializer or {}).get("max_wait_ms")
        if isinstance(write_serializer, dict)
        else None,
    }


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    rows = [
        "# Managed Claude Truth Probe",
        "",
        f"- Generated: `{utc_now()}`",
        f"- Session: `{summary.get('session_id') or '-'}`",
        "",
        "## Truth Surfaces",
        "",
        f"- Local managed Claude row: `{summary.get('local_health_has_managed_claude')}`",
        f"- Local state: `{summary.get('local_health_state') or '-'}`",
        f"- Local phase: `{summary.get('local_health_phase') or '-'}`",
        f"- Local phase ledger entries: `{summary.get('local_phase_ledger_entries')}`",
        f"- Latest phase ledger phase: `{summary.get('latest_phase_ledger_phase') or '-'}`",
        f"- Latest phase ledger source: `{summary.get('latest_phase_ledger_source') or '-'}`",
        f"- Latest phase ledger observed: `{summary.get('latest_phase_ledger_observed_at') or '-'}`",
        f"- Channel state file: `{summary.get('channel_state_exists')}`",
        f"- Channel ready: `{summary.get('channel_ready')}`",
        f"- Channel HTTP health: `{summary.get('channel_health_available')}`",
        f"- Claude PID alive: `{summary.get('claude_pid_alive')}`",
        f"- Bridge PID alive: `{summary.get('bridge_pid_alive')}`",
        f"- Hook outbox entries: `{summary.get('hook_outbox_entries')}`",
        f"- Latest hook state: `{summary.get('latest_hook_state') or '-'}`",
        f"- Latest hook mtime: `{summary.get('latest_hook_mtime') or '-'}`",
        f"- Hosted session exists: `{summary.get('hosted_session_exists')}`",
        f"- Hosted runtime phase: `{summary.get('hosted_runtime_phase') or '-'}`",
        f"- Hosted phase source: `{summary.get('hosted_phase_source') or '-'}`",
        f"- Hosted terminal state: `{summary.get('hosted_terminal_state') or '-'}`",
        f"- Hosted terminal reason: `{summary.get('hosted_terminal_reason') or '-'}`",
        f"- Hosted terminal source: `{summary.get('hosted_terminal_source') or '-'}`",
        f"- Hosted runtime updated: `{summary.get('hosted_runtime_updated_at') or '-'}`",
        f"- Hosted archive events: `{summary.get('hosted_archive_event_count')}`",
        f"- Hosted assistant archive events: `{summary.get('hosted_archive_assistant_events')}`",
        f"- Hosted transcript revision: `{summary.get('hosted_transcript_revision')}`",
        f"- Hosted runtime events: `{summary.get('hosted_runtime_event_count')}`",
        f"- Hosted terminal event count: `{summary.get('hosted_terminal_event_count')}`",
        f"- Hosted terminal event sources: `{', '.join(summary.get('hosted_terminal_event_sources') or []) or '-'}`",
        f"- Hosted WriteSerializer avg/max wait ms: `{summary.get('hosted_write_serializer_avg_wait_ms')}` / `{summary.get('hosted_write_serializer_max_wait_ms')}`",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", help="Managed Claude session id. Defaults to newest local managed Claude row.")
    parser.add_argument("--subdomain", default=os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN", "demo"), help="Hosted instance subdomain for hosted-session-debug.")
    parser.add_argument("--claude-dir", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--longhouse-home", type=Path, default=Path.home() / ".longhouse")
    parser.add_argument("--duration-secs", type=float, default=0.0, help="Repeat observations for this many seconds.")
    parser.add_argument("--interval-secs", type=float, default=1.0)
    parser.add_argument("--fast-local-health", action="store_true")
    parser.add_argument("--skip-hosted", action="store_true")
    parser.add_argument("--case-id", default="managed_claude_warm_live_graceful_close")
    parser.add_argument("--profile-class", default="warm_realtime")
    parser.add_argument("--run-id", default=f"managed-claude-truth-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / args.run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(output_dir / "observations.jsonl", args.run_id, args.case_id, args.profile_class)

    recorder.write(
        source="harness",
        event="probe_started",
        payload={
            "output_dir": str(output_dir),
            "subdomain": args.subdomain,
            "claude_dir": str(args.claude_dir),
            "longhouse_home": str(args.longhouse_home),
            "longhouse_available": command_available("longhouse"),
        },
    )

    deadline = time.monotonic() + max(0.0, args.duration_secs)
    selected_session_id: str | None = args.session_id
    selected_provider_session_id: str | None = None
    latest_summary: dict[str, Any] | None = None
    first = True
    logged_auto_selection = False
    while first or time.monotonic() < deadline:
        first = False
        local = collect_local_health(fast=args.fast_local_health)
        if not selected_session_id:
            selected_session_id = select_session_id(local, None)
            if selected_session_id and not logged_auto_selection:
                print(f"Auto-selected managed Claude session: {selected_session_id}")
                logged_auto_selection = True
        selected_provider_session_id = provider_session_id_from_local_health(local, selected_session_id)
        recorder.write(
            source="local_health",
            event="snapshot",
            session_id=selected_session_id,
            provider_session_id=selected_provider_session_id,
            payload=filter_local_health(local, selected_session_id) if selected_session_id else local,
        )

        channel_state = None
        channel_health = None
        hook_outbox = None
        if selected_session_id:
            state_path = state_file_for(selected_session_id, claude_dir=args.claude_dir)
            channel_state = read_json_file(state_path)
            if channel_state:
                selected_provider_session_id = str(channel_state.get("provider_session_id") or selected_provider_session_id or "").strip() or None
            recorder.write(
                source="claude_channel_state",
                event="snapshot",
                session_id=selected_session_id,
                provider_session_id=selected_provider_session_id,
                payload={"path": str(state_path), "mtime": file_mtime_iso(state_path), "state": channel_state},
            )
            hook_outbox = collect_hook_outbox(longhouse_home=args.longhouse_home, session_id=selected_session_id)
            recorder.write(
                source="hook_outbox",
                event="snapshot",
                session_id=selected_session_id,
                provider_session_id=selected_provider_session_id,
                payload=hook_outbox,
            )
            if channel_state:
                for key in ("claude_pid", "bridge_pid"):
                    recorder.write(
                        source="provider_process",
                        event=f"{key}_snapshot",
                        session_id=selected_session_id,
                        provider_session_id=selected_provider_session_id,
                        payload={"alive": pid_alive(channel_state.get(key)), "ps": ps_row(channel_state.get(key))},
                    )
                port = int(channel_state.get("port") or 0)
                if port > 0:
                    channel_health = http_json(f"http://127.0.0.1:{port}/health")
                    recorder.write(
                        source="claude_channel_http",
                        event="health",
                        session_id=selected_session_id,
                        provider_session_id=selected_provider_session_id,
                        payload=channel_health,
                    )

        hosted = None
        if selected_session_id and not args.skip_hosted:
            hosted = hosted_debug(selected_session_id, subdomain=args.subdomain, limit=20)
            recorder.write(
                source="hosted_debug",
                event="snapshot",
                session_id=selected_session_id,
                provider_session_id=selected_provider_session_id,
                payload=hosted,
            )

        latest_summary = summarize_probe(
            session_id=selected_session_id,
            local_health=local,
            channel_state=channel_state,
            channel_health=channel_health,
            hook_outbox=hook_outbox,
            hosted=hosted,
        )
        if time.monotonic() < deadline:
            time.sleep(max(0.1, args.interval_secs))

    if latest_summary is None:
        latest_summary = {"session_id": selected_session_id}
    summary_path = output_dir / "summary.md"
    write_summary(summary_path, latest_summary)
    (output_dir / "summary.json").write_text(json.dumps(redact(latest_summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(summary_path)
    if not selected_session_id:
        print("No managed Claude session found. Start one with `longhouse claude` or pass --session-id.", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
