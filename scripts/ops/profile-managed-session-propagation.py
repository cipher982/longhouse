#!/usr/bin/env python3
"""Profile Codex session propagation from local process truth to timeline truth.

This is the first implementation slice for
docs/specs/managed-session-propagation-profiler.md. It intentionally starts
with Codex because managed Codex has a bridge/control path that can be driven
without solving Claude's native-channel PTY lifecycle first.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import re
import select
import shlex
import subprocess
import sys
import threading
import time
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from managed_profiler.sla_manifest import DEFAULT_MANIFEST_PATH
from managed_profiler.sla_manifest import case_by_id
from managed_profiler.sla_manifest import format_case_inventory
from managed_profiler.sla_manifest import load_manifest
from managed_profiler.sla_manifest import manifest_summary
from managed_profiler.sla_manifest import metric_is_diagnostic
from managed_profiler.sla_manifest import metric_target_ms

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "managed-session-propagation"
BRIDGE_ROOT = Path.home() / ".longhouse" / "managed-local" / "codex-bridge"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
CODEX_HOOKS_JSON = Path.home() / ".codex" / "hooks.json"
CODEX_LONGHOUSE_HOOK_SCRIPT = Path.home() / ".codex" / "hooks" / "longhouse-codex-hook.sh"
BROWSER_UI_OBSERVER_SCRIPT = ROOT / "scripts" / "ops" / "managed_profiler" / "browser_ui_observer.mjs"
HOSTED_CONTAINER_PREFIX = "longhouse-"
HOSTED_RUNTIME_OBSERVATION_LIMIT = 200
METRICS_SCHEMA_VERSION = 3
BATCH_METRICS_SCHEMA_VERSION = 2
PENDING_BROWSER_SESSION_ID = "__pending_browser_session__"
BATCH_METRIC_KEYS = (
    "cold_timeline_navigation_to_card_paint_ms",
    "cold_timeline_navigation_to_close_paint_ms",
    "warm_session_created_to_card_paint_ms",
    "warm_live_output_local_to_paint_ms",
    "warm_live_output_sse_to_paint_ms",
    "warm_close_local_to_sse_ms",
    "warm_close_local_to_paint_ms",
    "warm_close_sse_to_paint_ms",
    "durable_archive_local_to_hosted_ms",
    "live_first_from_local_ms",
    "live_tail_non_slo_from_local_ms",
    "browser_workspace_stream_to_first_paint_ms",
    "browser_workspace_stream_to_tail_paint_ms",
    "browser_workspace_stream_after_sse_ms",
    "close_observed_ms",
    "bridge_live_ingest_lag_ms",
    "browser_timeline_card_from_session_id_ms",
    "ship_trace_prepare_open_db_ms",
    "ship_trace_prepare_binding_wait_ms",
    "ship_trace_prepare_parse_ms",
)
BATCH_VERDICT_SEVERITY = {
    "pass": 0,
    "contaminated": 1,
    "slow": 2,
    "partial": 3,
    "missing": 4,
    "blocked": 5,
    "provider_timeout": 5,
    "fail": 5,
    "error": 5,
}
BATCH_REQUIRED_FAIL_VERDICTS = frozenset(
    verdict
    for verdict, severity in BATCH_VERDICT_SEVERITY.items()
    if verdict not in {"pass", "contaminated"} and severity >= 1
)
BATCH_REQUIRED_INFRA_VERDICTS = frozenset({"contaminated"})
TRANSPORT_FAILURE_PATTERNS = (
    "Request timed out connecting",
    "status of 524",
    "status=524",
    "ReadTimeout",
    "ERR_QUIC_PROTOCOL_ERROR",
    "IPC stop timed out",
    "server responded with a status of 524",
)
CODEX_TUI_PRECONDITION_PATTERNS = (
    (
        re.compile(
            r"(?P<count>\d+)\s+hooks need review before they can run\. Open /hooks to review them\.",
            re.IGNORECASE,
        ),
        "codex_hooks_need_review",
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def slug_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@functools.cache
def sla_manifest() -> dict[str, Any]:
    return load_manifest(DEFAULT_MANIFEST_PATH)


def live_first_output_target_ms() -> int:
    return metric_target_ms(sla_manifest(), "live_first_from_local_ms", 500) or 500


def durable_archive_target_ms() -> int:
    return metric_target_ms(sla_manifest(), "durable_archive_local_to_hosted_ms", 3_000) or 3_000


def warm_session_created_target_ms() -> int:
    return metric_target_ms(sla_manifest(), "warm_session_created_to_card_paint_ms", 500) or 500


def managed_close_target_ms() -> int:
    return metric_target_ms(sla_manifest(), "close_observed_ms", 1_000) or 1_000


def cold_timeline_card_target_ms() -> int:
    return metric_target_ms(sla_manifest(), "cold_timeline_navigation_to_card_paint_ms", 2_000) or 2_000


def cold_timeline_close_target_ms() -> int:
    return metric_target_ms(sla_manifest(), "cold_timeline_navigation_to_close_paint_ms", 2_000) or 2_000


DEFAULT_SLA_CASE_BY_PROFILE_PROVIDER = {
    ("cold-timeline", "codex"): "managed_codex_cold_timeline_closed",
    ("warm-live", "codex"): "managed_codex_warm_live_graceful_close",
    ("warm-live", "claude"): "managed_claude_warm_live_graceful_close",
}


def default_sla_case_id(profile: str, provider: str) -> str | None:
    return DEFAULT_SLA_CASE_BY_PROFILE_PROVIDER.get((profile, provider))


def resolve_sla_case(args: argparse.Namespace) -> dict[str, Any] | None:
    case_id = args.sla_case or default_sla_case_id(args.profile, args.provider)
    if not case_id:
        return None
    case = case_by_id(sla_manifest(), case_id)
    if case is None:
        raise SystemExit(f"unknown --sla-case {case_id!r}")
    if case.get("status") == "undefined":
        raise SystemExit(f"--sla-case {case_id!r} is undefined and cannot be profiled")
    provider = case.get("provider")
    if provider not in {args.provider, "all"}:
        raise SystemExit(f"--sla-case {case_id!r} provider={provider!r} does not match --provider {args.provider!r}")
    if case.get("profile") not in {args.profile, "none"}:
        raise SystemExit(f"--sla-case {case_id!r} profile={case.get('profile')!r} does not match --profile {args.profile!r}")
    return case


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
        self.metrics_path = self.output_dir / "metrics.json"
        self.observations: list[dict[str, Any]] = []
        self.started_monotonic_ms = monotonic_ms()
        self.project = args.project
        self.subdomain = args.subdomain
        self.container = args.container or f"{HOSTED_CONTAINER_PREFIX}{self.subdomain}"
        self.browser_ui_base_url = args.browser_ui_base_url or f"https://{self.subdomain}.longhouse.ai"
        self.sla_case = resolve_sla_case(args)
        self.profile_class = args.profile_class or (
            self.sla_case.get("profile_class") if self.sla_case else profile_class_for(args.profile)
        )
        self.remote_clock_skew_ms = self.measure_remote_clock_skew_ms()
        self._observe_lock = threading.Lock()
        self._browser_session_cookie: str | None = None

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
            "profile_class": self.profile_class,
            "sla_case_id": self.sla_case.get("id") if self.sla_case else None,
            "sla_status": self.sla_case.get("status") if self.sla_case else None,
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
            "clock_skew_ms": self.remote_clock_skew_ms,
            "payload": payload or {},
        }
        with self._observe_lock:
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
            provider=self.args.provider,
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
            provider=self.args.provider,
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
            str(HOSTED_RUNTIME_OBSERVATION_LIMIT),
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
subdomain, sid, runtime_observation_limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
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
if table("session_observations"):
    runtime_rows = rows("SELECT id, source, observed_at, received_at, payload_json FROM session_observations WHERE session_id=? AND source_domain='runtime' ORDER BY id DESC LIMIT ?", (sid, runtime_observation_limit))
    payload["runtime_observations"] = []
    for row in runtime_rows:
        payload_json = row.pop("payload_json") or "{}"
        outer = json.loads(payload_json)
        inner = outer.get("payload") if isinstance(outer, dict) else {}
        if not isinstance(inner, dict):
            inner = {}
        row.update({
            "kind": outer.get("kind") if isinstance(outer, dict) else None,
            "phase": outer.get("phase") if isinstance(outer, dict) else None,
            "tool_name": outer.get("tool_name") if isinstance(outer, dict) else None,
            "occurred_at": row.get("observed_at"),
            "payload_json": json.dumps(inner, sort_keys=True),
        })
        payload["runtime_observations"].append(row)
print(json.dumps(payload, default=str))
"""
        proc = subprocess.run(
            [
                "ssh",
                self.args.ssh_target,
                "python3",
                "-",
                self.subdomain,
                session_id,
                str(HOSTED_RUNTIME_OBSERVATION_LIMIT),
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = safe_json_loads(proc.stdout)
        return data if isinstance(data, dict) else None

    def measure_remote_clock_skew_ms(self) -> int | None:
        cmd = [
            "ssh",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            self.args.ssh_target,
            "python3 -c 'import time; print(int(time.time()*1000))'",
        ]
        before = time.time() * 1000
        completed = run_cmd(cmd, timeout=10)
        after = time.time() * 1000
        if completed.returncode != 0:
            return None
        try:
            remote_ms = int((completed.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
        midpoint = (before + after) / 2
        return int(round(remote_ms - midpoint))

    def browser_session_cookie(self) -> str | None:
        if self._browser_session_cookie:
            return self._browser_session_cookie

        script = r"""
from zerg.auth.session_tokens import _issue_access_token
from zerg.database import db_session
from zerg.models.models import User

with db_session() as db:
    user = db.query(User).order_by(User.id.asc()).first()
    if user is None:
        raise SystemExit("no browser user found")
    print(_issue_access_token(user.id, user.email, display_name=user.display_name, avatar_url=user.avatar_url))
"""
        proc = subprocess.run(
            [
                "ssh",
                self.args.ssh_target,
                "docker",
                "exec",
                "-i",
                self.container,
                "python3",
                "-",
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        token = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        if proc.returncode != 0 or not token:
            return None
        self._browser_session_cookie = token
        return token

    def timeline_session(self, session_id: str) -> dict[str, Any] | None:
        token = self.browser_session_cookie()
        if not token:
            return {"error": "could not mint browser session cookie"}

        script = r"""
import json, sys, time
import httpx

token, sid, project = sys.argv[1], sys.argv[2], sys.argv[3]
headers = {"Cookie": f"longhouse_session={token}"}
client = httpx.Client(base_url="http://127.0.0.1:8000/api", headers=headers, timeout=10.0)

def get(path, **params):
    started = time.monotonic()
    response = client.get(path, params=params)
    elapsed = int((time.monotonic() - started) * 1000)
    body = None
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
    return response.status_code, elapsed, body, response.text[:500]

detail_status, detail_ms, detail_body, detail_text = get(f"/timeline/sessions/{sid}")
listing_status, listing_ms, listing_body, listing_text = get(
    "/timeline/sessions",
    project=project,
    provider=sys.argv[4],
    limit=20,
    hide_autonomous="true",
)
payload = {
    "detail_status": detail_status,
    "detail_request_ms": detail_ms,
    "listing_status": listing_status,
    "listing_request_ms": listing_ms,
}
if detail_status == 200:
    payload["detail"] = detail_body
else:
    payload["detail_error"] = detail_text
if listing_status == 200 and isinstance(listing_body, dict):
    data = listing_body
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
else:
    payload["listing_error"] = listing_text
print(json.dumps(payload, default=str))
"""
        proc = subprocess.run(
            [
                "ssh",
                self.args.ssh_target,
                "docker",
                "exec",
                "-i",
                self.container,
                "python3",
                "-",
                token,
                session_id,
                self.project,
                self.args.provider,
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
        token = self.browser_session_cookie()
        if not token:
            return {"error": "could not mint browser session cookie"}

        script = r"""
import json, sys, time
import httpx

token, sid, project, provider = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
headers = {"Cookie": f"longhouse_session={token}"}
params = {"project": project, "provider": provider, "limit": "20", "hide_autonomous": "true"}
seen = []
events = []
event_name = None
data_lines = []
contains_session = False
started = time.monotonic()

def flush_event():
    global event_name, data_lines
    if event_name is None and not data_lines:
        return False
    data = "\n".join(data_lines)
    events.append({"event": event_name, "data": data[:1000]})
    event_name = None
    data_lines = []
    return sid in data

timeout = httpx.Timeout(12.0, connect=3.0, read=12.0)
with httpx.stream(
    "GET",
    "http://127.0.0.1:8000/api/timeline/sessions/stream",
    params=params,
    headers=headers,
    timeout=timeout,
) as response:
    status_code = response.status_code
    for line in response.iter_lines():
        if sid in line:
            contains_session = True
        seen.append(line[:1000])
        if line == "":
            contains_session = flush_event() or contains_session
            if contains_session or len(seen) > 120:
                break
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        if sid in line or len(seen) > 120:
            break

payload = {
    "status_code": status_code,
    "line_count": len(seen),
    "contains_session": contains_session or any(sid in event.get("data", "") for event in events),
    "elapsed_ms": int((time.monotonic() - started) * 1000),
    "events": events[:8],
    "sample": seen[:20],
}
print(json.dumps(payload))
"""
        proc = subprocess.run(
            [
                "ssh",
                self.args.ssh_target,
                "docker",
                "exec",
                "-i",
                self.container,
                "python3",
                "-",
                token,
                session_id,
                self.project,
                self.args.provider,
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=15,
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
                    provider=self.args.provider,
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
            provider=self.args.provider,
            ownership=ownership,
            source="hosted_db",
            event=f"{event}_timeout",
            session_id=session_id,
            payload=compact_hosted(last or {}),
        )
        return last

    def poll_timeline_session(
        self,
        session_id: str,
        *,
        case_id: str,
        ownership: str,
        predicate,
        event: str,
        timeout: float = 30,
        interval: float = 0.25,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            data = self.timeline_session(session_id)
            last = data if isinstance(data, dict) else None
            if last is not None and predicate(last):
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_http",
                    event=event,
                    session_id=session_id,
                    payload=compact_timeline(last),
                )
                return last
            time.sleep(interval)
        self.observe(
            case_id=case_id,
            provider=self.args.provider,
            ownership=ownership,
            source="hosted_http",
            event=f"{event}_timeout",
            session_id=session_id,
            payload=compact_timeline(last or {}),
        )
        return last

    def poll_timeline_transcript_preview(
        self,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
        timeout: float = 90,
        interval: float = 0.1,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        first_observed = False

        while time.monotonic() < deadline:
            data = self.timeline_session(session_id)
            last = data if isinstance(data, dict) else None
            transcripts = timeline_transcript_previews(last or {})
            if transcripts and not first_observed:
                first_observed = True
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_http",
                    event="timeline_transcript_preview_first_visible",
                    session_id=session_id,
                    payload=compact_timeline(last or {}),
                )
            if last is not None and timeline_transcript_preview_contains(last, nonce):
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_http",
                    event="timeline_transcript_preview_visible",
                    session_id=session_id,
                    payload=compact_timeline(last),
                )
                return last
            time.sleep(interval)

        if not first_observed:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="hosted_http",
                event="timeline_transcript_preview_first_visible_timeout",
                session_id=session_id,
                payload=compact_timeline(last or {}),
            )
        self.observe(
            case_id=case_id,
            provider=self.args.provider,
            ownership=ownership,
            source="hosted_http",
            event="timeline_transcript_preview_visible_timeout",
            session_id=session_id,
            payload=compact_timeline(last or {}),
        )
        return last

    def stream_timeline_transcript_preview_sse(
        self,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
    ) -> None:
        token = self.browser_session_cookie()
        if not token:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="hosted_sse",
                event="timeline_transcript_preview_sse_first_timeout",
                session_id=session_id,
                payload={"error": "could not mint browser session cookie"},
            )
            return

        script = r"""
import json, sys, time
import httpx

token, sid, project, nonce = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
headers = {"Cookie": f"longhouse_session={token}"}
params = {"skip_initial": "true"}
event_name = None
data_lines = []
first_observed = False
started = time.monotonic()

def transcript_preview_from_event(data):
    try:
        obj = json.loads(data)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("session_id") != sid:
        return None
    preview = obj.get("transcript_preview")
    if isinstance(preview, dict) and preview.get("text"):
        return preview
    return None

def emit(kind, preview=None, error=None):
    payload = {
        "kind": kind,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "transcript_preview": preview,
        "error": error,
    }
    print(json.dumps(payload, default=str), flush=True)

def flush_event():
    global event_name, data_lines, first_observed
    if event_name is None and not data_lines:
        return False
    current_event = event_name
    data = "\n".join(data_lines)
    event_name = None
    data_lines = []
    if current_event != "workspace_changed":
        return False
    preview = transcript_preview_from_event(data)
    if not preview:
        return False
    text = str(preview.get("text") or "")
    if not first_observed:
        first_observed = True
        emit("first", preview)
    if nonce in text:
        emit("full", preview)
        return True
    return False

try:
    timeout = httpx.Timeout(95.0, connect=3.0, read=95.0)
    with httpx.stream(
        "GET",
        f"http://127.0.0.1:8000/api/timeline/sessions/{sid}/workspace/stream",
        params=params,
        headers=headers,
        timeout=timeout,
    ) as response:
        if response.status_code != 200:
            emit("error", error=f"status={response.status_code} body={response.text[:500]}")
            raise SystemExit(0)
        emit("ready")
        deadline = time.monotonic() + 90
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                break
            if line == "":
                if flush_event():
                    raise SystemExit(0)
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        flush_event()
        emit("timeout" if first_observed else "first_timeout")
except SystemExit:
    raise
except Exception as exc:
    emit("error", error=repr(exc))
"""
        proc = subprocess.Popen(
            [
                "ssh",
                self.args.ssh_target,
                "docker",
                "exec",
                "-i",
                self.container,
                "python3",
                "-",
                token,
                session_id,
                self.project,
                nonce,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write(script)
        proc.stdin.close()

        saw_first = False
        saw_full = False
        for line in proc.stdout:
            data = safe_json_loads(line.strip())
            if not isinstance(data, dict):
                continue
            kind = data.get("kind")
            if kind == "ready":
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_sse",
                    event="timeline_transcript_preview_sse_ready",
                    session_id=session_id,
                    payload=data,
                )
            elif kind == "first":
                saw_first = True
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_sse",
                    event="timeline_transcript_preview_sse_first_visible",
                    session_id=session_id,
                    payload=data,
                )
            elif kind == "full":
                saw_full = True
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_sse",
                    event="timeline_transcript_preview_sse_visible",
                    session_id=session_id,
                    payload=data,
                )
                break
            elif kind in {"first_timeout", "timeout", "error"}:
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_sse",
                    event=f"timeline_transcript_preview_sse_{kind}",
                    session_id=session_id,
                    payload=data,
                )
                break

        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

        stderr = proc.stderr.read().strip()
        if not saw_first:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="hosted_sse",
                event="timeline_transcript_preview_sse_first_visible_timeout",
                session_id=session_id,
                payload={"returncode": proc.returncode, "stderr": stderr[-1000:]},
            )
        elif not saw_full:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="hosted_sse",
                event="timeline_transcript_preview_sse_visible_timeout",
                session_id=session_id,
                payload={"returncode": proc.returncode, "stderr": stderr[-1000:]},
            )

    def stream_timeline_close_sse(
        self,
        session_id: str,
        *,
        case_id: str,
        ownership: str,
    ) -> None:
        token = self.browser_session_cookie()
        if not token:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="hosted_sse",
                event="timeline_close_sse_timeout",
                session_id=session_id,
                payload={"error": "could not mint browser session cookie"},
            )
            return

        script = r"""
import json, sys, time
import httpx

token, sid, project, provider = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
headers = {"Cookie": f"longhouse_session={token}"}
params = {
    "project": project,
    "provider": provider,
    "limit": "20",
    "hide_autonomous": "true",
    "skip_initial_replay": "true",
}
event_name = None
data_lines = []
started = time.monotonic()

def session_from_event(data):
    try:
        obj = json.loads(data)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    session = obj.get("session")
    if isinstance(session, dict) and session.get("thread_id") == sid:
        return session
    return None

def is_closed(session):
    candidates = [session, session.get("head") if isinstance(session, dict) else None]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("ended_at"):
            return True
        status = str(candidate.get("status") or "").lower()
        if status in {"completed", "closed"}:
            return True
        runtime = candidate.get("runtime_display")
        if isinstance(runtime, dict) and str(runtime.get("lifecycle") or "").lower() == "closed":
            return True
    return False

def emit(kind, session=None, error=None):
    payload = {
        "kind": kind,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "session": session,
        "error": error,
    }
    print(json.dumps(payload, default=str), flush=True)

def flush_event():
    global event_name, data_lines
    if event_name is None and not data_lines:
        return False
    current_event = event_name
    data = "\n".join(data_lines)
    event_name = None
    data_lines = []
    if current_event != "session_upsert":
        return False
    session = session_from_event(data)
    if not session or not is_closed(session):
        return False
    emit("closed", session)
    return True

try:
    timeout = httpx.Timeout(15.0, connect=3.0, read=15.0)
    with httpx.stream(
        "GET",
        "http://127.0.0.1:8000/api/timeline/sessions/stream",
        params=params,
        headers=headers,
        timeout=timeout,
    ) as response:
        if response.status_code != 200:
            emit("error", error=f"status={response.status_code} body={response.text[:500]}")
            raise SystemExit(0)
        emit("ready")
        deadline = time.monotonic() + 10
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                break
            if line == "":
                if flush_event():
                    raise SystemExit(0)
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        flush_event()
        emit("timeout")
except SystemExit:
    raise
except Exception as exc:
    emit("error", error=repr(exc))
"""
        proc = subprocess.Popen(
            [
                "ssh",
                self.args.ssh_target,
                "docker",
                "exec",
                "-i",
                self.container,
                "python3",
                "-",
                token,
                session_id,
                self.project,
                self.args.provider,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write(script)
        proc.stdin.close()

        saw_closed = False
        saw_terminal = False
        for line in proc.stdout:
            data = safe_json_loads(line.strip())
            if not isinstance(data, dict):
                continue
            kind = data.get("kind")
            if kind == "ready":
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_sse",
                    event="timeline_close_sse_ready",
                    session_id=session_id,
                    payload=data,
                )
            elif kind == "closed":
                saw_closed = True
                saw_terminal = True
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_sse",
                    event="timeline_close_sse_visible",
                    session_id=session_id,
                    payload=data,
                )
                break
            elif kind in {"timeout", "error"}:
                saw_terminal = True
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_sse",
                    event=f"timeline_close_sse_{kind}",
                    session_id=session_id,
                    payload=data,
                )
                break

        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

        stderr = proc.stderr.read().strip()
        if not saw_closed and not saw_terminal:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="hosted_sse",
                event="timeline_close_sse_timeout",
                session_id=session_id,
                payload={"returncode": proc.returncode, "stderr": stderr[-1000:]},
            )

    def observe_browser_ui(
        self,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
        observer_kind: str = "warm",
        session_id_file: Path | None = None,
    ) -> None:
        token = self.browser_session_cookie()
        if not token:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="browser_ui",
                event="browser_ui_error",
                session_id=session_id,
                payload={"error": "could not mint browser session cookie"},
            )
            return

        current_session_id: str | None = None if session_id == "-" else session_id
        script_path = self.output_dir / f"{session_id}-browser-ui-observer.mjs"
        script_path.write_text(BROWSER_UI_OBSERVER_SCRIPT.read_text())
        env = os.environ.copy()
        if session_id_file is not None:
            env["LONGHOUSE_BROWSER_OBSERVER_SESSION_ID_FILE"] = str(session_id_file)
        if self.args.profile == "warm-live" and observer_kind == "warm":
            env["LONGHOUSE_BROWSER_OBSERVER_EXIT_AFTER_DETAIL_TRANSCRIPT"] = "1"

        proc = subprocess.Popen(
            [
                "bun",
                str(script_path),
                self.browser_ui_base_url,
                token,
                session_id,
                self.project,
                nonce,
                self.args.provider,
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        event_map = {
            "navigation_started": "browser_ui_navigation_started",
            "ui_loaded": "browser_ui_loaded",
            "card_painted": "browser_timeline_card_painted",
            "preview_first_painted": "browser_transcript_preview_first_painted",
            "preview_word_painted": "browser_transcript_preview_word_painted",
            "preview_nonce_painted": "browser_transcript_preview_nonce_painted",
            "close_painted": "browser_close_card_painted",
            "awaiting_session_id": "browser_awaiting_session_id",
            "session_id_received": "browser_session_id_received",
            "detail_navigation_started": "browser_detail_navigation_started",
            "detail_loaded": "browser_detail_loaded",
            "timeline_page_closed_after_card": "browser_timeline_page_closed_after_card",
            "timeline_stream_connected": "browser_timeline_stream_connected",
            "timeline_stream_heartbeat": "browser_timeline_stream_heartbeat",
            "timeline_stream_session_upsert": "browser_timeline_stream_session_upsert",
            "timeline_stream_session_remove": "browser_timeline_stream_session_remove",
            "timeline_stream_workspace_connected": "browser_workspace_stream_connected",
            "timeline_stream_workspace_changed": "browser_workspace_stream_changed",
            "timeline_stream_workspace_preview_changed": "browser_workspace_preview_stream_changed",
            "live_transcript_first_painted": "browser_live_transcript_first_painted",
            "live_transcript_nonce_painted": "browser_live_transcript_nonce_painted",
        }
        timeout_map = {
            "card_painted_timeout": "browser_timeline_card_painted_timeout",
            "preview_first_painted_timeout": "browser_transcript_preview_first_painted_timeout",
            "preview_word_painted_timeout": "browser_transcript_preview_word_painted_timeout",
            "preview_nonce_painted_timeout": "browser_transcript_preview_nonce_painted_timeout",
            "close_painted_timeout": "browser_close_card_painted_timeout",
            "live_transcript_first_painted_timeout": "browser_live_transcript_first_painted_timeout",
            "live_transcript_nonce_painted_timeout": "browser_live_transcript_nonce_painted_timeout",
        }
        if observer_kind == "cold":
            event_map = {
                key: value.replace("browser_", "browser_cold_", 1)
                for key, value in event_map.items()
            }
            timeout_map = {
                key: value.replace("browser_", "browser_cold_", 1)
                for key, value in timeout_map.items()
            }
        for line in proc.stdout:
            data = safe_json_loads(line.strip())
            if not isinstance(data, dict):
                continue
            kind = str(data.get("kind") or "")
            event = event_map.get(kind) or timeout_map.get(kind)
            if event is None and kind in {"console", "page_error", "error"}:
                event = f"browser_{'cold_' if observer_kind == 'cold' else ''}ui_{kind}"
            if event is None:
                continue
            if kind == "session_id_received":
                received = data.get("session_id")
                if isinstance(received, str) and received:
                    current_session_id = received
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="browser_ui",
                event=event,
                session_id=current_session_id or PENDING_BROWSER_SESSION_ID,
                payload=data,
            )
            if event in {"browser_close_card_painted", "browser_cold_close_card_painted"}:
                break

        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

        stderr = proc.stderr.read().strip()
        if proc.returncode not in {0, None}:
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="browser_ui",
                event="browser_ui_error",
                session_id=session_id,
                payload={"returncode": proc.returncode, "stderr": stderr[-1000:], "script": str(script_path)},
            )

    def start_timeline_live_poll(
        self,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: self.poll_timeline_transcript_preview(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
            ),
            name=f"timeline-live-poll-{session_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def start_timeline_live_sse(
        self,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: self.stream_timeline_transcript_preview_sse(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
            ),
            name=f"timeline-live-sse-{session_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def start_browser_ui_observer(
        self,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
        observer_kind: str = "warm",
        session_id_file: Path | None = None,
    ) -> threading.Thread | None:
        if self.args.skip_browser_ui:
            return None
        thread = threading.Thread(
            target=lambda: self.observe_browser_ui(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                observer_kind=observer_kind,
                session_id_file=session_id_file,
            ),
            name=f"browser-{observer_kind}-ui-{session_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def start_timeline_close_sse(
        self,
        session_id: str,
        *,
        case_id: str,
        ownership: str,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: self.stream_timeline_close_sse(
                session_id,
                case_id=case_id,
                ownership=ownership,
            ),
            name=f"timeline-close-sse-{session_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def run_managed_codex(self) -> dict[str, Any]:
        case_id = "B1"
        ownership = "managed"
        nonce = f"LH_PROBE_CODEX_MANAGED_{self.run_id}"
        name = f"{self.args.name_prefix}-managed-{self.run_id}"
        self.browser_session_cookie()
        self.prepare_codex_hooks(case_id=case_id, ownership=ownership)
        browser_ui = None
        staged_session_id_file: Path | None = None
        if self.args.profile == "warm-live" and not self.args.skip_browser_ui:
            staged_session_id_file = self.output_dir / "browser-session-id.txt"
            try:
                staged_session_id_file.unlink()
            except FileNotFoundError:
                pass
            browser_ui = self.start_browser_ui_observer(
                "-",
                nonce,
                case_id=case_id,
                ownership=ownership,
                session_id_file=staged_session_id_file,
            )
            browser_ready = self.wait_for_observation(
                case_id,
                PENDING_BROWSER_SESSION_ID,
                "browser_ui_loaded",
                timeout=30,
            )
            stream_ready = self.wait_for_observation(
                case_id,
                PENDING_BROWSER_SESSION_ID,
                "browser_timeline_stream_connected",
                timeout=10,
            )
            if not browser_ready or not stream_ready:
                raise RuntimeError(
                    "warm browser observer did not reach ready state before managed launch: "
                    f"browser_ready={browser_ready} stream_ready={stream_ready}"
                )
        self.observe(
            case_id=case_id,
            provider=self.args.provider,
            ownership=ownership,
            source="harness",
            event="launch_requested",
            payload={"nonce": nonce, "name": name},
        )
        launch_cmd = [
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
        ]
        if self.args.codex_model:
            launch_cmd.extend(["--model", self.args.codex_model])
        if self.args.codex_effort:
            launch_cmd.extend(["--model-reasoning-effort", self.args.codex_effort])
        launch = self.run_observed(
            launch_cmd,
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
        if staged_session_id_file is not None:
            staged_session_id_file.write_text(session_id + "\n")
        if self.args.profile not in {"cold-timeline", "warm-live"}:
            browser_ui = self.start_browser_ui_observer(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
            )
        self.poll_timeline_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=timeline_has_card,
            event="timeline_card_visible_pre_ingest",
            timeout=30,
            interval=0.25,
        )
        self.write_snapshot(case_id, ownership, session_id, "post_launch")

        tui_log = self.output_dir / f"{session_id}-managed-tui.log"
        tui_cmd = [
            "/opt/homebrew/bin/codex",
            "-c",
            "check_for_update_on_startup=false",
        ]
        if self.args.codex_effort:
            tui_cmd.extend(["-c", f"model_reasoning_effort={self.args.codex_effort}"])
        if self.args.codex_model:
            tui_cmd.extend(["--model", self.args.codex_model])
        tui_cmd.extend(["--enable", "tui_app_server", "--remote", ws_url, "--no-alt-screen"])
        remote_exec = f"LONGHOUSE_MANAGED_SESSION_ID={shlex.quote(session_id)} exec {shlex.join(tui_cmd)}"
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
        precondition = self.wait_codex_tui_precondition(
            tui_log,
            case_id=case_id,
            ownership=ownership,
            session_id=session_id,
            timeout=8,
        )
        if precondition:
            self.write_snapshot(case_id, ownership, session_id, "provider_precondition")
            self.observe(
                case_id=case_id,
                provider="codex",
                ownership=ownership,
                source="harness",
                event="shutdown_requested",
                session_id=session_id,
                payload={"reason": "provider_precondition"},
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
            if browser_ui is not None:
                browser_ui.join(timeout=150)
            self.write_snapshot(case_id, ownership, session_id, "post_shutdown")
            return {
                "case_id": case_id,
                "session_id": session_id,
                "nonce": nonce,
                "thread_id": thread_id,
                "thread_path": str(thread_path) if thread_path else None,
                "precondition": precondition,
            }

        timeline_live_poll = None
        timeline_live_sse = None
        if self.args.profile != "cold-timeline":
            timeline_live_poll = self.start_timeline_live_poll(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
            )
            timeline_live_sse = self.start_timeline_live_sse(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
            )
        if self.args.profile == "warm-live":
            browser_ready = self.wait_for_observation(
                case_id,
                session_id,
                "browser_timeline_card_painted",
                timeout=30,
            )
            sse_ready = self.wait_for_observation(
                case_id,
                session_id,
                "timeline_transcript_preview_sse_ready",
                timeout=10,
            )
            if browser_ready and sse_ready:
                self.observe(
                    case_id=case_id,
                    provider="codex",
                    ownership=ownership,
                    source="harness",
                    event="warm_ready_at",
                    session_id=session_id,
                    payload={
                        "browser_card_ready": True,
                        "timeline_sse_ready": True,
                    },
                )
            else:
                self.observe(
                    case_id=case_id,
                    provider="codex",
                    ownership=ownership,
                    source="harness",
                    event="provider_precondition_blocked",
                    session_id=session_id,
                    payload={
                        "reason": "warm_live_precondition_timeout",
                        "browser_card_ready": browser_ready,
                        "timeline_sse_ready": sse_ready,
                    },
                )
                self.observe(
                    case_id=case_id,
                    provider="codex",
                    ownership=ownership,
                    source="harness",
                    event="shutdown_requested",
                    session_id=session_id,
                    payload={"reason": "warm_live_precondition_timeout"},
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
                if browser_ui is not None:
                    browser_ui.join(timeout=150)
                self.write_snapshot(case_id, ownership, session_id, "warm_ready_timeout")
                return {
                    "case_id": case_id,
                    "session_id": session_id,
                    "nonce": nonce,
                    "thread_id": thread_id,
                    "thread_path": str(thread_path) if thread_path else None,
                    "precondition": {
                        "reason": "warm_live_precondition_timeout",
                        "browser_card_ready": browser_ready,
                        "timeline_sse_ready": sse_ready,
                    },
                }
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
        local_assistant_event = None
        if thread_path:
            local_assistant_event = self.poll_local_assistant_response(
                thread_path,
                nonce,
                case_id=case_id,
                ownership=ownership,
                session_id=session_id,
            )
        if local_assistant_event is None:
            self.observe(
                case_id=case_id,
                provider="codex",
                ownership=ownership,
                source="provider_transcript",
                event="provider_response_timeout",
                session_id=session_id,
                payload={"thread_path": str(thread_path) if thread_path else None},
            )
        else:
            self.poll_hosted_session(
                session_id,
                case_id=case_id,
                ownership=ownership,
                predicate=lambda data: hosted_assistant_events_contain(data, nonce),
                event="assistant_response_hosted",
                timeout=180,
                interval=0.5,
            )
        if timeline_live_poll is not None:
            timeline_live_poll.join(timeout=95)
        if timeline_live_sse is not None:
            timeline_live_sse.join(timeout=95)
        self.write_snapshot(case_id, ownership, session_id, "post_response")

        timeline_close_sse = self.start_timeline_close_sse(
            session_id,
            case_id=case_id,
            ownership=ownership,
        )
        close_sse_ready = self.wait_for_observation(
            case_id,
            session_id,
            "timeline_close_sse_ready",
            timeout=10,
        )
        if not close_sse_ready:
            self.observe(
                case_id=case_id,
                provider="codex",
                ownership=ownership,
                source="harness",
                event="timeline_close_sse_precondition_timeout",
                session_id=session_id,
            )
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
        timeline_close_sse.join(timeout=12)
        if self.args.profile == "cold-timeline":
            browser_ui = self.start_browser_ui_observer(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                observer_kind="cold",
            )
        if browser_ui is not None:
            browser_ui.join(timeout=150)
        self.write_snapshot(case_id, ownership, session_id, "post_shutdown")
        return {
            "case_id": case_id,
            "session_id": session_id,
            "nonce": nonce,
            "thread_id": thread_id,
            "thread_path": str(thread_path) if thread_path else None,
        }

    def run_managed_claude(self) -> dict[str, Any]:
        case_id = "C1"
        ownership = "managed"
        nonce = f"LH_PROBE_CLAUDE_MANAGED_{self.run_id}"
        name = f"{self.args.name_prefix}-managed-claude-{self.run_id}"
        session_id_file = self.output_dir / "claude-browser-session-id.txt"
        poc_dir = self.output_dir / "claude-poc"
        poc_dir.mkdir(parents=True, exist_ok=True)
        try:
            session_id_file.unlink()
        except FileNotFoundError:
            pass

        self.browser_session_cookie()
        browser_ui = None
        if self.args.profile == "warm-live" and not self.args.skip_browser_ui:
            browser_ui = self.start_browser_ui_observer(
                "-",
                nonce,
                case_id=case_id,
                ownership=ownership,
                session_id_file=session_id_file,
            )
            browser_ready = self.wait_for_observation(
                case_id,
                PENDING_BROWSER_SESSION_ID,
                "browser_ui_loaded",
                timeout=30,
            )
            stream_ready = self.wait_for_observation(
                case_id,
                PENDING_BROWSER_SESSION_ID,
                "browser_timeline_stream_connected",
                timeout=10,
            )
            if not browser_ready:
                raise RuntimeError(
                    "warm browser observer did not reach ready state before managed Claude launch: "
                    f"browser_ready={browser_ready} stream_ready={stream_ready}"
                )
            self.observe(
                case_id=case_id,
                provider="claude",
                ownership=ownership,
                source="harness",
                event="warm_ready_at",
                payload={
                    "browser_loaded": browser_ready,
                    "timeline_stream_connected": stream_ready,
                },
            )

        self.observe(
            case_id=case_id,
            provider="claude",
            ownership=ownership,
            source="harness",
            event="launch_requested",
            payload={"nonce": nonce, "name": name},
        )
        cmd = [
            str(ROOT / "scripts" / "ops" / "run-managed-claude-poc.py"),
            "--cwd",
            str(ROOT),
            "--project",
            self.project,
            "--name",
            name,
            "--prompt",
            f"Reply with exactly {nonce}",
            "--expected",
            nonce,
            "--run-id",
            f"{self.run_id}-claude",
            "--output-dir",
            str(poc_dir),
            "--response-timeout-secs",
            "90",
            "--post-close-probe-secs",
            "5",
            "--skip-live-probe",
            "--skip-post-close-probe",
            "--session-id-file",
            str(session_id_file),
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.observe(
            case_id=case_id,
            provider="claude",
            ownership=ownership,
            source="harness",
            event="managed_launch_started",
            payload={"cmd": redact_cmd(cmd), "poc_dir": str(poc_dir)},
        )

        events_path = poc_dir / "events.jsonl"
        imported_count = 0
        session_id: str | None = None
        observed_expected = False
        while proc.poll() is None:
            imported_count, session_id, observed_expected = self.import_claude_poc_events(
                events_path,
                case_id=case_id,
                ownership=ownership,
                imported_count=imported_count,
                session_id=session_id,
                observed_expected=observed_expected,
            )
            time.sleep(0.1)
        stdout, stderr = proc.communicate()
        imported_count, session_id, observed_expected = self.import_claude_poc_events(
            events_path,
            case_id=case_id,
            ownership=ownership,
            imported_count=imported_count,
            session_id=session_id,
            observed_expected=observed_expected,
        )
        summary = read_json(poc_dir / "summary.json") or {}
        session_id = session_id or str(summary.get("session_id") or "").strip() or None
        observed_expected = observed_expected or bool(summary.get("observed_expected"))
        self.observe(
            case_id=case_id,
            provider="claude",
            ownership=ownership,
            source="harness",
            event="managed_launch_completed",
            session_id=session_id,
            payload={
                "returncode": proc.returncode,
                "stdout": (stdout or "")[-1000:],
                "stderr": (stderr or "")[-1000:],
                "summary": summary,
            },
        )
        if not session_id:
            raise RuntimeError(f"managed Claude POC did not report a session id: returncode={proc.returncode}")
        if proc.returncode != 0 or not observed_expected:
            raise RuntimeError(
                "managed Claude POC failed: "
                f"returncode={proc.returncode} observed_expected={observed_expected}"
            )

        self.poll_timeline_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=timeline_has_card,
            event="timeline_card_visible_pre_ingest",
            timeout=30,
            interval=0.25,
        )
        self.poll_hosted_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=lambda data: hosted_assistant_events_contain(data, nonce),
            event="assistant_response_hosted",
            timeout=60,
            interval=0.25,
        )
        self.poll_hosted_session(
            session_id,
            case_id=case_id,
            ownership=ownership,
            predicate=lambda data: lifecycle_closed(data),
            event="hosted_runtime_closed",
            timeout=30,
            interval=0.25,
        )
        if browser_ui is not None:
            browser_ui.join(timeout=150)
        self.write_snapshot(case_id, ownership, session_id, "post_claude")
        return {
            "case_id": case_id,
            "session_id": session_id,
            "nonce": nonce,
            "poc_dir": str(poc_dir),
            "precondition": None,
        }

    def import_claude_poc_events(
        self,
        path: Path,
        *,
        case_id: str,
        ownership: str,
        imported_count: int,
        session_id: str | None,
        observed_expected: bool,
    ) -> tuple[int, str | None, bool]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return imported_count, session_id, observed_expected
        for line in lines[imported_count:]:
            row = safe_json_loads(line)
            if not isinstance(row, dict):
                continue
            event = str(row.get("event") or "")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            row_session_id = str(payload.get("session_id") or session_id or "").strip() or None
            wall = row.get("observed_at_wall")
            if event == "session_id_observed" and row_session_id:
                session_id = row_session_id
                self.observe(
                    case_id=case_id,
                    provider="claude",
                    ownership=ownership,
                    source="claude_poc",
                    event="session_id_observed",
                    session_id=session_id,
                    provider_session_id=session_id,
                    payload={"poc_event": event, "observed_at_wall": wall, **payload},
                )
            elif event == "prompt_sent" and row_session_id:
                self.observe(
                    case_id=case_id,
                    provider="claude",
                    ownership=ownership,
                    source="claude_poc",
                    event="prompt_sent_started",
                    session_id=row_session_id,
                    payload={"timestamp": wall, "poc_event": event, **payload},
                )
            elif event == "assistant_transcript_observed" and row_session_id:
                observed_expected = True
                timestamp = payload.get("transcript_timestamp") or wall
                self.observe(
                    case_id=case_id,
                    provider="claude",
                    ownership=ownership,
                    source="provider_transcript",
                    event="assistant_response_local",
                    session_id=row_session_id,
                    payload={"timestamp": timestamp, "poc_event": event, "observed_at_wall": wall, **payload},
                )
            elif event in {"exit_sent", "exit_sent_after_timeout"} and row_session_id:
                self.observe(
                    case_id=case_id,
                    provider="claude",
                    ownership=ownership,
                    source="claude_poc",
                    event="shutdown_requested",
                    session_id=row_session_id,
                    payload={"timestamp": wall, "poc_event": event, **payload},
                )
            elif event == "process_exit_final" and row_session_id:
                self.observe(
                    case_id=case_id,
                    provider="claude",
                    ownership=ownership,
                    source="claude_poc",
                    event="provider_process_exit_observed",
                    session_id=row_session_id,
                    payload={"timestamp": wall, "poc_event": event, **payload},
                )
        return len(lines), session_id, observed_expected

    def prepare_codex_hooks(self, *, case_id: str, ownership: str) -> None:
        result = self.probe_codex_longhouse_hooks(trust=self.args.trust_longhouse_codex_hooks)
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="codex_app_server",
            event="codex_hook_preflight",
            payload=summarize_codex_hook_probe(result),
        )

    def probe_codex_longhouse_hooks(self, *, trust: bool) -> dict[str, Any]:
        with CodexAppServerProbe(cwd=ROOT) as probe:
            before = probe.longhouse_hooks()
            writes: dict[str, dict[str, str]] = {}
            for hook in before:
                if not is_expected_longhouse_codex_hook(hook):
                    continue
                if hook.get("trustStatus") not in {"untrusted", "modified"}:
                    continue
                current_hash = str(hook.get("currentHash") or "")
                key = str(hook.get("key") or "")
                if current_hash and key:
                    writes[key] = {"trusted_hash": current_hash}

            write_result = None
            after = before
            if trust and writes:
                write_result = probe.request(
                    "config/batchWrite",
                    {
                        "edits": [
                            {
                                "keyPath": "hooks.state",
                                "value": writes,
                                "mergeStrategy": "upsert",
                            }
                        ],
                        "reloadUserConfig": True,
                    },
                )
                after = probe.longhouse_hooks()
            return {
                "trusted_requested": trust,
                "before": before,
                "after": after,
                "trusted_written": len(writes) if trust else 0,
                "write_status": (write_result or {}).get("status") if isinstance(write_result, dict) else None,
            }

    def wait_codex_tui_precondition(
        self,
        path: Path,
        *,
        case_id: str,
        ownership: str,
        session_id: str,
        timeout: float,
        interval: float = 0.25,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        last_size = None
        while time.monotonic() < deadline:
            precondition = find_codex_tui_precondition(path)
            if precondition is not None:
                self.observe(
                    case_id=case_id,
                    provider="codex",
                    ownership=ownership,
                    source="provider_tui",
                    event="provider_precondition_blocked",
                    session_id=session_id,
                    payload={"path": str(path), **precondition},
                )
                return precondition
            try:
                last_size = path.stat().st_size
            except OSError:
                last_size = None
            time.sleep(interval)
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="provider_tui",
            event="provider_precondition_clear",
            session_id=session_id,
            payload={"path": str(path), "last_size": last_size},
        )
        return None

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

    def write_summary(self, results: list[dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        lines = [
            "# Managed Session Propagation Profile",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Profile: `{self.args.profile}` (`{self.profile_class}`)",
            f"- Started: `{utc_now()}`",
            f"- Project: `{self.project}`",
            f"- Subdomain: `{self.subdomain}`",
            f"- Observations: `{self.observations_path}`",
            f"- Metrics: `{self.metrics_path}`",
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
            verdict, notes, case_metrics = self.verdict_for(case_id, sid, nonce)
            metrics.append(case_metrics)
            lines.append(f"| {case_id} | `{sid}` | `{nonce}` | {verdict} | {notes} |")
        if errors:
            lines.extend(["", "## Errors", ""])
            lines.extend(f"- {err}" for err in errors)
        lines.extend(["", "## Artifact Directory", "", f"`{self.output_dir}`", ""])
        self.summary_path.write_text("\n".join(lines))
        self.metrics_path.write_text(
            json.dumps(
                {
                    "schema_version": METRICS_SCHEMA_VERSION,
                    "run_id": self.run_id,
                    "profile_class": self.profile_class,
                    "sla_case_id": self.sla_case.get("id") if self.sla_case else None,
                    "sla_status": self.sla_case.get("status") if self.sla_case else None,
                    "project": self.project,
                    "subdomain": self.subdomain,
                    "generated_at": utc_now(),
                    "targets": {
                        "live_first_output_ms": live_first_output_target_ms(),
                        "durable_archive_ms": durable_archive_target_ms(),
                        "managed_close_ms": managed_close_target_ms(),
                    },
                    "sla_manifest": {
                        "path": str(DEFAULT_MANIFEST_PATH),
                        "summary": manifest_summary(sla_manifest()),
                    },
                    "errors": errors,
                    "cases": metrics,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return metrics

    def verdict_for(self, case_id: str, session_id: str, nonce: str) -> tuple[str, str, dict[str, Any]]:
        active_metrics = set(self.sla_case.get("metrics") or []) if self.sla_case else set()
        requires_create = not active_metrics or "warm_session_created_to_card_paint_ms" in active_metrics
        requires_live = not active_metrics or any(
            metric in active_metrics
            for metric in (
                "warm_live_output_local_to_paint_ms",
                "warm_live_output_sse_to_paint_ms",
            )
        )
        requires_close = not active_metrics or any(
            metric in active_metrics
            for metric in (
                "warm_close_local_to_sse_ms",
                "warm_close_local_to_paint_ms",
                "warm_close_sse_to_paint_ms",
                "warm_close_local_to_db_ms",
            )
        )
        requires_cold = any(
            metric in active_metrics
            for metric in (
                "cold_timeline_navigation_to_card_paint_ms",
                "cold_timeline_navigation_to_close_paint_ms",
            )
        )
        requires_durable = not active_metrics or "durable_archive_local_to_hosted_ms" in active_metrics
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
        card_latency = self.event_delta_ms(
            case_id,
            session_id,
            "session_id_observed",
            "timeline_card_visible_pre_ingest",
        )
        browser_card_latency = self.event_delta_ms(
            case_id,
            session_id,
            "session_id_observed",
            "browser_timeline_card_painted",
        )
        if browser_card_latency is None:
            browser_card_latency = self.event_wall_delta_ms(
                case_id,
                session_id,
                "session_id_observed",
                "browser_timeline_card_painted",
            )
        live_http_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "timeline_transcript_preview_visible",
        )
        first_live_http_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "timeline_transcript_preview_first_visible",
        )
        live_http_from_local_latency = self.event_delta_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "timeline_transcript_preview_visible",
        )
        first_live_http_from_local_latency = self.event_delta_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "timeline_transcript_preview_first_visible",
        )
        live_sse_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "timeline_transcript_preview_sse_visible",
        )
        first_live_sse_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "timeline_transcript_preview_sse_first_visible",
        )
        live_sse_from_local_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "timeline_transcript_preview_sse_visible",
        )
        first_live_sse_from_local_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "timeline_transcript_preview_sse_first_visible",
        )
        browser_live_first_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "browser_transcript_preview_first_painted",
        )
        browser_live_full_latency = self.event_delta_ms(
            case_id,
            session_id,
            "prompt_sent_started",
            "browser_transcript_preview_nonce_painted",
        )
        browser_live_first_from_local_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "browser_transcript_preview_first_painted",
        )
        browser_live_full_from_local_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "browser_transcript_preview_nonce_painted",
        )
        browser_live_first_from_local_wall_latency = self.event_wall_delta_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "browser_live_transcript_first_painted",
        )
        browser_live_full_from_local_wall_latency = self.event_wall_delta_ms(
            case_id,
            session_id,
            "assistant_response_local",
            "browser_live_transcript_nonce_painted",
        )
        browser_live_first_from_live_truth_wall_latency = self.event_wall_delta_ms(
            case_id,
            session_id,
            "timeline_live_transcript_sse_first_visible",
            "browser_live_transcript_first_painted",
        )
        browser_live_full_from_live_truth_wall_latency = self.event_wall_delta_ms(
            case_id,
            session_id,
            "timeline_live_transcript_sse_visible",
            "browser_live_transcript_nonce_painted",
        )
        browser_first_after_sse_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "timeline_transcript_preview_sse_first_visible",
            "browser_live_transcript_first_painted",
        )
        if browser_first_after_sse_latency is None:
            browser_first_after_sse_latency = self.event_delta_any_order_ms(
                case_id,
                session_id,
                "timeline_transcript_preview_sse_first_visible",
                "browser_transcript_preview_first_painted",
            )
        browser_full_after_sse_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "timeline_transcript_preview_sse_visible",
            "browser_live_transcript_nonce_painted",
        )
        if browser_full_after_sse_latency is None:
            browser_full_after_sse_latency = self.event_delta_any_order_ms(
                case_id,
                session_id,
                "timeline_transcript_preview_sse_visible",
                "browser_transcript_preview_nonce_painted",
            )
        workspace_stream_event = (
            "browser_workspace_preview_stream_changed"
            if self.event_observed_at_ms(case_id, session_id, "browser_workspace_preview_stream_changed") is not None
            else "browser_workspace_stream_changed"
        )
        browser_workspace_to_first_paint_latency = self.event_payload_elapsed_delta_nearest_before_ms(
            case_id,
            session_id,
            workspace_stream_event,
            "browser_live_transcript_first_painted",
        )
        if browser_workspace_to_first_paint_latency is None:
            browser_workspace_to_first_paint_latency = self.event_delta_any_order_ms(
                case_id,
                session_id,
                workspace_stream_event,
                "browser_live_transcript_first_painted",
            )
        browser_workspace_to_tail_paint_latency = self.event_payload_elapsed_delta_nearest_before_ms(
            case_id,
            session_id,
            workspace_stream_event,
            "browser_live_transcript_nonce_painted",
        )
        if browser_workspace_to_tail_paint_latency is None:
            browser_workspace_to_tail_paint_latency = self.event_delta_any_order_ms(
                case_id,
                session_id,
                workspace_stream_event,
                "browser_live_transcript_nonce_painted",
            )
        browser_workspace_after_sse_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "timeline_transcript_preview_sse_first_visible",
            workspace_stream_event,
        )
        warm_ready_to_prompt_latency = self.event_delta_ms(
            case_id,
            session_id,
            "warm_ready_at",
            "prompt_sent_started",
        )
        terminal = terminal_details(hosted)
        transcript_ingest = transcript_ingest_details(hosted, self.remote_clock_skew_ms)
        durable_archive_latency = (
            transcript_ingest.get("skew_adjusted_lag_ms")
            if transcript_ingest.get("skew_adjusted_lag_ms") is not None
            else transcript_ingest.get("ingest_lag_ms")
            if transcript_ingest.get("ingest_lag_ms") is not None
            else propagation_latency
        )
        close_http_latency = self.event_delta_ms(
            case_id,
            session_id,
            "shutdown_requested",
            "hosted_runtime_closed",
        )
        close_backend_latency = self.terminal_received_delta_from_event_ms(
            case_id,
            session_id,
            terminal,
            "shutdown_requested",
        )
        close_sse_ready_before_shutdown = (
            self.event_delta_ms(
                case_id,
                session_id,
                "timeline_close_sse_ready",
                "shutdown_requested",
            )
            is not None
        )
        close_sse_latency = (
            self.event_delta_ms(
                case_id,
                session_id,
                "shutdown_requested",
                "timeline_close_sse_visible",
            )
            if close_sse_ready_before_shutdown
            else None
        )
        close_browser_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "shutdown_requested",
            "browser_close_card_painted",
        )
        close_browser_after_http_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "hosted_runtime_closed",
            "browser_close_card_painted",
        )
        close_browser_after_sse_latency = (
            self.event_delta_any_order_ms(
                case_id,
                session_id,
                "timeline_close_sse_visible",
                "browser_close_card_painted",
            )
            if close_sse_ready_before_shutdown
            else None
        )
        cold_card_latency = self.event_delta_ms(
            case_id,
            session_id,
            "browser_cold_ui_navigation_started",
            "browser_cold_timeline_card_painted",
        )
        cold_close_latency = self.event_delta_ms(
            case_id,
            session_id,
            "browser_cold_ui_navigation_started",
            "browser_cold_close_card_painted",
        )
        cold_card_to_close_latency = self.event_delta_ms(
            case_id,
            session_id,
            "browser_cold_timeline_card_painted",
            "browser_cold_close_card_painted",
        )
        close_latency = (
            close_browser_latency
            if close_browser_latency is not None
            else close_sse_latency
            if close_sse_latency is not None
            else close_backend_latency
            if close_backend_latency is not None
            else close_http_latency
        )
        close_source = (
            "browser_ui"
            if close_browser_latency is not None
            else "sse"
            if close_sse_latency is not None
            else "hosted_terminal"
            if close_backend_latency is not None
            else "http"
        )
        latest_health = self.latest_local_health_summary(case_id, session_id)
        latest_health_state = latest_health.get("health_state")
        transport_failure = self.transport_failure_classification(case_id, session_id, latest_health_state)
        ownership = session.get("execution_home") or "-"
        transport = session.get("managed_transport") or "-"
        metrics: dict[str, Any] = {
            "case_id": case_id,
            "profile_class": self.profile_class,
            "session_id": session_id,
            "nonce": nonce,
            "ownership": ownership,
            "transport": transport,
            "provider": session.get("provider") or self.args.provider,
            "live_first_from_local_ms": None,
            "live_first_target_ms": live_first_output_target_ms(),
            "live_first_pass": None,
            "live_first_source": None,
            "live_tail_non_slo_from_local_ms": None,
            "warm_session_created_to_card_paint_ms": browser_card_latency,
            "warm_session_created_target_ms": warm_session_created_target_ms(),
            "warm_session_created_pass": (
                browser_card_latency <= warm_session_created_target_ms()
                if browser_card_latency is not None
                else None
            ),
            "warm_live_output_local_to_paint_ms": None,
            "warm_live_output_sse_to_paint_ms": browser_first_after_sse_latency,
            "warm_close_local_to_sse_ms": close_sse_latency,
            "warm_close_local_to_paint_ms": close_latency,
            "warm_close_sse_to_paint_ms": close_browser_after_sse_latency,
            "cold_timeline_navigation_to_card_paint_ms": cold_card_latency,
            "cold_timeline_navigation_to_close_paint_ms": cold_close_latency,
            "cold_timeline_card_to_close_paint_ms": cold_card_to_close_latency,
            "cold_timeline_card_target_ms": cold_timeline_card_target_ms(),
            "cold_timeline_close_target_ms": cold_timeline_close_target_ms(),
            "cold_timeline_card_pass": (
                cold_card_latency <= cold_timeline_card_target_ms()
                if cold_card_latency is not None
                else None
            ),
            "cold_timeline_close_pass": (
                cold_close_latency <= cold_timeline_close_target_ms()
                if cold_close_latency is not None
                else None
            ),
            "browser_timeline_card_from_session_id_ms": browser_card_latency,
            "browser_live_first_from_prompt_ms": browser_live_first_latency,
            "browser_live_tail_from_prompt_ms": browser_live_full_latency,
            "browser_live_first_from_local_raw_ms": browser_live_first_from_local_latency,
            "browser_live_tail_from_local_raw_ms": browser_live_full_from_local_latency,
            "browser_live_first_from_local_wall_ms": browser_live_first_from_local_wall_latency,
            "browser_live_tail_from_local_wall_ms": browser_live_full_from_local_wall_latency,
            "browser_live_first_from_live_truth_wall_ms": browser_live_first_from_live_truth_wall_latency,
            "browser_live_tail_from_live_truth_wall_ms": browser_live_full_from_live_truth_wall_latency,
            "browser_live_first_after_sse_raw_ms": browser_first_after_sse_latency,
            "browser_live_tail_after_sse_raw_ms": browser_full_after_sse_latency,
            "browser_workspace_stream_event": workspace_stream_event,
            "browser_workspace_stream_to_first_paint_ms": browser_workspace_to_first_paint_latency,
            "browser_workspace_stream_to_tail_paint_ms": browser_workspace_to_tail_paint_latency,
            "browser_workspace_stream_after_sse_ms": browser_workspace_after_sse_latency,
            "warm_ready_to_prompt_ms": warm_ready_to_prompt_latency,
            "warm_live_prompt_to_sse_first_ms": first_live_sse_latency,
            "warm_live_prompt_to_browser_first_paint_ms": browser_live_first_latency,
            "warm_live_sse_to_browser_first_paint_ms": browser_first_after_sse_latency,
            "durable_archive_local_to_hosted_ms": durable_archive_latency,
            "durable_archive_target_ms": durable_archive_target_ms(),
            "durable_archive_pass": None,
            "close_observed_ms": close_latency,
            "close_source": close_source if close_latency is not None else None,
            "close_http_observed_ms": close_http_latency,
            "close_backend_observed_ms": close_backend_latency,
            "close_sse_observed_ms": close_sse_latency,
            "close_browser_observed_ms": close_browser_latency,
            "close_browser_after_http_raw_ms": close_browser_after_http_latency,
            "close_browser_after_sse_raw_ms": close_browser_after_sse_latency,
            "close_target_ms": managed_close_target_ms(),
            "close_pass": None,
            "bridge_live_ingest_lag_ms": None,
            "bridge_live_skew_adjusted_lag_ms": None,
            "bridge_live_method": None,
            "ship_trace_source": None,
            "ship_trace_wake_reason": None,
            "ship_trace_prepare_open_db_ms": None,
            "ship_trace_prepare_binding_wait_ms": None,
            "ship_trace_prepare_parse_ms": None,
            "failure_classification": transport_failure,
            "local_health_state": latest_health_state,
            "provider_timeout": self.event_observed_at_ms(
                case_id,
                session_id,
                "provider_response_timeout",
            )
            is not None
            or self.event_observed_at_ms(
                case_id,
                session_id,
                "assistant_response_local_timeout",
            )
            is not None,
        }
        if not session:
            metrics["verdict"] = "missing"
            metrics["notes"] = "hosted session row not observed"
            return "missing", "hosted session row not observed", metrics
        precondition = self.provider_precondition_for(case_id, session_id)

        live_first_from_local_latency = (
            browser_live_first_from_live_truth_wall_latency
            if browser_live_first_from_live_truth_wall_latency is not None
            else browser_live_first_from_local_wall_latency
            if browser_live_first_from_local_wall_latency is not None
            else browser_live_first_from_local_latency
        )
        live_full_from_local_latency = (
            browser_live_full_from_live_truth_wall_latency
            if browser_live_full_from_live_truth_wall_latency is not None
            else browser_live_full_from_local_wall_latency
            if browser_live_full_from_local_wall_latency is not None
            else browser_live_full_from_local_latency
        )
        live_ui_source = "browser_ui"
        live_timing_source = (
            "live_transcript_occurred_at"
            if browser_live_first_from_live_truth_wall_latency is not None
            else "payload_wall"
            if browser_live_first_from_local_wall_latency is not None
            else "harness_observed"
        )
        if live_first_from_local_latency is None:
            live_first_from_local_latency = first_live_sse_from_local_latency
            live_full_from_local_latency = live_sse_from_local_latency
            live_ui_source = "sse"
            live_timing_source = "harness_observed"
        if live_first_from_local_latency is None:
            live_first_from_local_latency = first_live_http_from_local_latency
            live_full_from_local_latency = live_http_from_local_latency
            live_ui_source = "http"
            live_timing_source = "harness_observed"
        metrics["live_first_from_local_ms"] = live_first_from_local_latency
        metrics["warm_live_output_local_to_paint_ms"] = live_first_from_local_latency
        metrics["live_first_source"] = live_ui_source if live_first_from_local_latency is not None else None
        metrics["live_first_timing_source"] = live_timing_source if live_first_from_local_latency is not None else None
        metrics["live_tail_non_slo_from_local_ms"] = live_full_from_local_latency
        if live_first_from_local_latency is not None:
            metrics["live_first_pass"] = live_first_from_local_latency <= live_first_output_target_ms()

        create_note = "create=not_applicable"
        if requires_create:
            if browser_card_latency is None:
                create_note = "create=missing"
            else:
                create_state = "pass" if browser_card_latency <= warm_session_created_target_ms() else "slow"
                create_note = (
                    f"create={create_state} card_from_session_id={browser_card_latency}ms "
                    f"target={warm_session_created_target_ms()}ms"
                )

        live_ui = "live_first=missing"
        if requires_cold and not requires_live:
            live_ui = "live_first=not_applicable"
        if live_first_from_local_latency is not None:
            live_state = (
                "pass"
                if live_first_from_local_latency <= live_first_output_target_ms()
                else "slow"
            )
            live_ui = (
                f"live_first={live_state} "
                f"source={live_ui_source} "
                f"timing={live_timing_source} "
                f"first_from_local={live_first_from_local_latency}ms "
                f"target={live_first_output_target_ms()}ms"
            )
            if live_full_from_local_latency is not None:
                live_ui += f" live_tail_non_slo_from_local={live_full_from_local_latency}ms"

        cold_note = "cold=not_run"
        if requires_cold:
            cold_parts = []
            if cold_card_latency is not None:
                card_state = "pass" if cold_card_latency <= cold_timeline_card_target_ms() else "slow"
                cold_parts.append(
                    f"card={card_state} nav_to_card={cold_card_latency}ms target={cold_timeline_card_target_ms()}ms"
                )
            else:
                cold_parts.append("card=missing")
            if cold_close_latency is not None:
                close_state = "pass" if cold_close_latency <= cold_timeline_close_target_ms() else "slow"
                cold_parts.append(
                    f"close={close_state} nav_to_close={cold_close_latency}ms target={cold_timeline_close_target_ms()}ms"
                )
            else:
                cold_parts.append("close=missing")
            if cold_card_to_close_latency is not None:
                cold_parts.append(f"card_to_close={cold_card_to_close_latency}ms")
            cold_note = "cold=" + ",".join(cold_parts)

        transcript = "synced" if contains else "missing"
        if transcript_latency is not None:
            transcript += f" observed_in={transcript_latency}ms"
        if provider_latency is not None:
            transcript += f" provider={provider_latency}ms"
        if durable_archive_latency is not None:
            transcript += f" local_to_hosted={durable_archive_latency}ms"
            if propagation_latency is not None and propagation_latency != durable_archive_latency:
                transcript += f" poll_observed={propagation_latency}ms"
        if card_latency is not None:
            transcript += f" timeline_card_pre_ingest={card_latency}ms"
        if browser_card_latency is not None:
            transcript += f" browser_card_from_session_id={browser_card_latency}ms"
        if first_live_http_latency is not None:
            transcript += f" first_live_http={first_live_http_latency}ms"
        if live_http_latency is not None:
            transcript += f" live_http={live_http_latency}ms"
        if first_live_sse_latency is not None:
            transcript += f" first_live_sse={first_live_sse_latency}ms"
        if live_sse_latency is not None:
            transcript += f" live_sse={live_sse_latency}ms"
        if browser_live_first_latency is not None:
            transcript += f" browser_first_live={browser_live_first_latency}ms"
        if browser_live_full_latency is not None:
            transcript += f" browser_live={browser_live_full_latency}ms"
        if first_live_http_from_local_latency is not None:
            transcript += f" first_live_http_from_local={first_live_http_from_local_latency}ms"
        if live_http_from_local_latency is not None:
            transcript += f" live_http_from_local={live_http_from_local_latency}ms"
        if first_live_sse_from_local_latency is not None:
            transcript += f" first_live_sse_from_local={first_live_sse_from_local_latency}ms"
        if live_sse_from_local_latency is not None:
            transcript += f" live_sse_from_local={live_sse_from_local_latency}ms"
        if browser_live_first_from_local_latency is not None:
            transcript += f" browser_first_live_from_local={browser_live_first_from_local_latency}ms"
        if browser_live_full_from_local_latency is not None:
            transcript += f" browser_live_from_local={browser_live_full_from_local_latency}ms"
        if browser_live_first_from_local_wall_latency is not None:
            transcript += f" browser_first_live_from_local_wall={browser_live_first_from_local_wall_latency}ms"
        if browser_live_full_from_local_wall_latency is not None:
            transcript += f" browser_live_from_local_wall={browser_live_full_from_local_wall_latency}ms"
        if browser_live_first_from_live_truth_wall_latency is not None:
            transcript += f" browser_first_live_from_live_truth={browser_live_first_from_live_truth_wall_latency}ms"
        if browser_live_full_from_live_truth_wall_latency is not None:
            transcript += f" browser_live_from_live_truth={browser_live_full_from_live_truth_wall_latency}ms"
        if browser_first_after_sse_latency is not None:
            transcript += f" sse_to_browser_first_live={browser_first_after_sse_latency}ms"
        if browser_full_after_sse_latency is not None:
            transcript += f" sse_to_browser_live={browser_full_after_sse_latency}ms"
        if browser_workspace_to_first_paint_latency is not None:
            transcript += f" browser_workspace_stream_to_first_paint={browser_workspace_to_first_paint_latency}ms"
        if browser_workspace_to_tail_paint_latency is not None:
            transcript += f" browser_workspace_stream_to_tail_paint={browser_workspace_to_tail_paint_latency}ms"
        if browser_workspace_after_sse_latency is not None:
            transcript += f" browser_workspace_stream_after_sse={browser_workspace_after_sse_latency}ms"
        if transcript_ingest.get("ingest_lag_ms") is not None:
            transcript += f" server_ingest_lag={transcript_ingest['ingest_lag_ms']}ms"
        if transcript_ingest.get("skew_adjusted_lag_ms") is not None:
            transcript += f" skew_adjusted_ingest={transcript_ingest['skew_adjusted_lag_ms']}ms"
        if durable_archive_latency is not None:
            durable_state = "pass" if durable_archive_latency <= durable_archive_target_ms() else "slow"
            metrics["durable_archive_pass"] = durable_archive_latency <= durable_archive_target_ms()
            transcript += f" durable_archive={durable_state} target={durable_archive_target_ms()}ms"
        bridge_live = bridge_live_details(hosted, nonce, self.remote_clock_skew_ms)
        if bridge_live:
            metrics["bridge_live_ingest_lag_ms"] = bridge_live.get("ingest_lag_ms")
            metrics["bridge_live_skew_adjusted_lag_ms"] = bridge_live.get("skew_adjusted_lag_ms")
            metrics["bridge_live_method"] = bridge_live.get("method")
            live_parts = []
            for key, label in (
                ("ingest_lag_ms", "ingest_lag"),
                ("skew_adjusted_lag_ms", "skew_adjusted"),
            ):
                if bridge_live.get(key) is not None:
                    live_parts.append(f"{label}={bridge_live[key]}ms")
            method = bridge_live.get("method")
            if method:
                live_parts.insert(0, f"method={method}")
            if live_parts:
                transcript += " bridge_live=" + ",".join(live_parts)
        ship_trace = ship_trace_details(hosted, self.remote_clock_skew_ms)
        if ship_trace:
            parts = []
            source = ship_trace.get("observation_source")
            if source:
                metrics["ship_trace_source"] = source
                parts.append(f"source={source}")
            wake_reason = ship_trace.get("wake_reason")
            if wake_reason:
                metrics["ship_trace_wake_reason"] = wake_reason
                parts.append(f"wake={wake_reason}")
            for key, label in (
                ("append_to_job_ms", "append_to_job"),
                ("observation_to_enqueue_ms", "observe_to_enqueue"),
                ("observation_to_wake_ms", "observe_to_wake"),
                ("wake_to_enqueue_ms", "wake_to_enqueue"),
                ("enqueue_to_job_ms", "enqueue_to_job"),
                ("observed_to_job_ms", "observed_to_job"),
                ("prepare_ms", "prepare"),
                ("prepare_open_db_ms", "open_db"),
                ("prepare_binding_wait_ms", "binding_wait"),
                ("prepare_parse_ms", "parse"),
                ("job_to_http_ms", "job_to_http"),
                ("http_to_handler_ms", "http_to_handler"),
                ("store_write_ms", "store"),
            ):
                if ship_trace.get(key) is not None:
                    parts.append(f"{label}={ship_trace[key]}ms")
            for key in (
                "prepare_open_db_ms",
                "prepare_binding_wait_ms",
                "prepare_parse_ms",
            ):
                if ship_trace.get(key) is not None:
                    metrics[f"ship_trace_{key}"] = ship_trace[key]
            if parts:
                transcript += " ship_trace=" + ",".join(parts)
        close_note = "close=missing"
        if closed:
            close_note = "close=closed"
            if close_latency is not None:
                close_note += f" source={close_source} observed_in={close_latency}ms"
                close_state = "pass" if close_latency <= managed_close_target_ms() else "slow"
                metrics["close_pass"] = close_latency <= managed_close_target_ms()
                close_note += f" close_slo={close_state} target={managed_close_target_ms()}ms"
                if close_sse_latency is not None and close_http_latency is not None:
                    close_note += f" http_observed_in={close_http_latency}ms"
                if close_browser_latency is not None:
                    close_note += f" browser_observed_in={close_browser_latency}ms"
                if close_browser_after_http_latency is not None:
                    close_note += f" http_to_browser={close_browser_after_http_latency}ms"
                if close_browser_after_sse_latency is not None:
                    close_note += f" sse_to_browser={close_browser_after_sse_latency}ms"
            if terminal.get("ingest_lag_ms") is not None:
                close_note += f" ingest_lag={terminal['ingest_lag_ms']}ms"
            if terminal.get("source"):
                close_note += f" source={terminal['source']}"
            if terminal.get("reason"):
                close_note += f" reason={terminal['reason']}"
        if precondition:
            reason = precondition.get("reason") or "provider_precondition"
            message = precondition.get("message") or ""
            note = f"provider_precondition={reason}"
            if message:
                note += f" message={message!r}"
            metrics["precondition"] = precondition
            metrics["verdict"] = "blocked"
            metrics["notes"] = f"{note}; {close_note}; ownership={ownership}, transport={transport}"
            return "blocked", metrics["notes"], metrics
        if metrics["provider_timeout"]:
            metrics["verdict"] = "provider_timeout"
            metrics["notes"] = (
                f"provider_timeout=true; {live_ui}; transcript={transcript}; "
                f"{close_note}; ownership={ownership}, transport={transport}"
            )
            return "provider_timeout", metrics["notes"], metrics
        if transport_failure is not None and (
            (requires_create and metrics["warm_session_created_pass"] is not True)
            or
            (requires_live and metrics["live_first_pass"] is not True)
            or (
                requires_cold
                and (
                    metrics["cold_timeline_card_pass"] is not True
                    or metrics["cold_timeline_close_pass"] is not True
                )
            )
            or (requires_durable and not contains)
        ):
            metrics["verdict"] = "contaminated"
            metrics["notes"] = (
                f"{live_ui}; {cold_note}; transcript={transcript}; {close_note}; "
                f"transport_failure={transport_failure}; ownership={ownership}, transport={transport}"
            )
            return "contaminated", metrics["notes"], metrics
        if requires_durable and not contains:
            verdict = "partial" if closed else "missing"
            metrics["verdict"] = verdict
            metrics["notes"] = f"{live_ui}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            return verdict, metrics["notes"], metrics
        is_managed_case = case_id == "B1" or ownership == "managed_local"
        if requires_create and is_managed_case and metrics["warm_session_created_pass"] is not True:
            verdict = "missing" if browser_card_latency is None else "slow"
            metrics["verdict"] = verdict
            metrics["notes"] = (
                f"{create_note}; {live_ui}; transcript={transcript}; {close_note}; "
                f"ownership={ownership}, transport={transport}"
            )
            return verdict, metrics["notes"], metrics
        if requires_live and is_managed_case and metrics["live_first_pass"] is not True:
            metrics["verdict"] = "fail"
            metrics["notes"] = (
                f"{create_note}; {live_ui}; transcript={transcript}; {close_note}; "
                f"ownership={ownership}, transport={transport}"
            )
            return "fail", metrics["notes"], metrics
        if requires_cold and (
            metrics["cold_timeline_card_pass"] is not True
            or metrics["cold_timeline_close_pass"] is not True
        ):
            verdict = (
                "missing"
                if cold_card_latency is None or cold_close_latency is None
                else "slow"
            )
            metrics["verdict"] = verdict
            metrics["notes"] = f"{live_ui}; {cold_note}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            return verdict, metrics["notes"], metrics
        if requires_close and not closed:
            phase = runtime.get("phase") or runtime.get("terminal_state") or "-"
            if transport_failure is not None:
                metrics["verdict"] = "contaminated"
                metrics["notes"] = (
                    f"{live_ui}; nonce synced; close not confirmed yet; "
                    f"transport_failure={transport_failure}; local_health={latest_health_state}; phase={phase}; "
                    f"ownership={ownership}, transport={transport}"
                )
                return "contaminated", metrics["notes"], metrics
            metrics["verdict"] = "partial"
            metrics["notes"] = f"{live_ui}; nonce synced; close not confirmed yet; phase={phase}; ownership={ownership}, transport={transport}"
            return "partial", metrics["notes"], metrics
        if transport_failure is not None and (
            (requires_close and metrics["close_pass"] is False)
            or (requires_durable and metrics["durable_archive_pass"] is False)
        ):
            metrics["verdict"] = "contaminated"
            metrics["notes"] = (
                f"{live_ui}; transcript={transcript}; {close_note}; "
                f"transport_failure={transport_failure}; ownership={ownership}, transport={transport}"
            )
            return "contaminated", metrics["notes"], metrics
        if requires_close and is_managed_case and metrics["close_pass"] is False:
            metrics["verdict"] = "slow"
            metrics["notes"] = f"{live_ui}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            return "slow", metrics["notes"], metrics
        if requires_durable and metrics["durable_archive_pass"] is False:
            metrics["verdict"] = "slow"
            metrics["notes"] = f"{live_ui}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            return "slow", metrics["notes"], metrics
        metrics["verdict"] = "pass"
        extra = f"; {cold_note}" if requires_cold else ""
        create_extra = f"{create_note}; " if requires_create else ""
        metrics["notes"] = (
            f"{create_extra}{live_ui}{extra}; transcript={transcript}; {close_note}; "
            f"ownership={ownership}, transport={transport}"
        )
        return "pass", metrics["notes"], metrics

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

    def event_delta_any_order_ms(
        self,
        case_id: str,
        session_id: str,
        start_event: str,
        end_event: str,
    ) -> int | None:
        start = self.event_observed_at_ms(case_id, session_id, start_event)
        end = self.event_observed_at_ms(case_id, session_id, end_event)
        if start is None or end is None:
            return None
        return end - start

    def event_observed_at_ms(self, case_id: str, session_id: str, event: str) -> int | None:
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") == event:
                observed = row.get("observed_at_monotonic_ms")
                if isinstance(observed, int):
                    return observed
        return None

    def event_wall_delta_ms(
        self,
        case_id: str,
        session_id: str,
        start_event: str,
        end_event: str,
    ) -> int | None:
        start = self.event_payload_wall_ms(case_id, session_id, start_event)
        end = self.event_payload_wall_ms(case_id, session_id, end_event)
        if start is None or end is None:
            return None
        return end - start

    def event_payload_wall_ms(self, case_id: str, session_id: str, event: str) -> int | None:
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") != event:
                continue
            timestamp = payload_wall_timestamp(row)
            if timestamp is not None:
                return timestamp
        return None

    def event_payload_elapsed_delta_ms(
        self,
        case_id: str,
        session_id: str,
        start_event: str,
        end_event: str,
    ) -> int | None:
        start = self.event_payload_elapsed_ms(case_id, session_id, start_event)
        end = self.event_payload_elapsed_ms(case_id, session_id, end_event)
        if start is None or end is None:
            return None
        return end - start

    def event_payload_elapsed_ms(self, case_id: str, session_id: str, event: str) -> int | None:
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") != event:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            value = payload.get("elapsed_ms")
            if isinstance(value, int | float):
                return int(value)
        return None

    def event_payload_elapsed_delta_nearest_before_ms(
        self,
        case_id: str,
        session_id: str,
        start_event: str,
        end_event: str,
    ) -> int | None:
        end = self.event_payload_elapsed_ms(case_id, session_id, end_event)
        if end is None:
            return None
        starts: list[int] = []
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") != start_event:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            value = payload.get("elapsed_ms")
            if isinstance(value, int | float):
                elapsed = int(value)
                if elapsed <= end:
                    starts.append(elapsed)
        if not starts:
            return None
        return end - max(starts)

    def terminal_received_delta_from_event_ms(
        self,
        case_id: str,
        session_id: str,
        terminal: dict[str, Any],
        start_event: str,
    ) -> int | None:
        start = self.event_payload_wall_ms(case_id, session_id, start_event)
        received = terminal.get("received_at_ms")
        if start is None or not isinstance(received, int):
            return None
        delta = received - start
        if self.remote_clock_skew_ms is not None:
            delta -= self.remote_clock_skew_ms
        return max(0, delta)

    def wait_for_observation(
        self,
        case_id: str,
        session_id: str,
        event: str,
        *,
        timeout: float,
        interval: float = 0.05,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.event_observed_at_ms(case_id, session_id, event) is not None:
                return True
            time.sleep(interval)
        return self.event_observed_at_ms(case_id, session_id, event) is not None

    def provider_precondition_for(self, case_id: str, session_id: str) -> dict[str, Any] | None:
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") == "provider_precondition_blocked":
                payload = row.get("payload")
                if isinstance(payload, dict):
                    return payload
                return {}
        return None

    def latest_local_health_summary(self, case_id: str, session_id: str) -> dict[str, Any]:
        for row in reversed(self.observations):
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            local_health = payload.get("local_health")
            if not isinstance(local_health, dict):
                continue
            summary = local_health.get("summary")
            if isinstance(summary, dict):
                return summary
        return {}

    def transport_failure_classification(
        self,
        case_id: str,
        session_id: str,
        latest_health_state: Any,
    ) -> str | None:
        _ = latest_health_state
        local_degraded = False
        hosted_degraded = False
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_text = json.dumps(payload, sort_keys=True, default=str)
            if row.get("event") == "shutdown_completed" and any(
                marker in payload_text for marker in TRANSPORT_FAILURE_PATTERNS
            ):
                local_degraded = True
            if row.get("source") in {"hosted_http", "hosted_sse", "browser_ui"} and any(
                marker in payload_text for marker in TRANSPORT_FAILURE_PATTERNS
            ):
                hosted_degraded = True
        if local_degraded and hosted_degraded:
            return "both_degraded"
        if local_degraded:
            return "local_transport_degraded"
        if hosted_degraded:
            return "hosted_transport_degraded"
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


def profile_class_for(profile: str) -> str:
    if profile == "cold-timeline":
        return "cold_timeline"
    if profile == "warm-live":
        return "warm_realtime"
    return "warm_realtime"


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


def payload_wall_timestamp(row: dict[str, Any]) -> int | None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for path in payload_wall_candidate_paths(str(row.get("event") or "")):
        value = nested_get(payload, path)
        parsed = parse_iso_wall_ms(value)
        if parsed is not None:
            return parsed
    return None


def payload_wall_candidate_paths(event: str) -> tuple[tuple[str, ...], ...]:
    if event == "assistant_response_local":
        return (("timestamp",),)
    if event in {"session_id_observed", "prompt_sent_started", "shutdown_requested"}:
        return (("observed_at_wall",), ("timestamp",))
    if event.startswith("timeline_live_transcript_sse"):
        return (
            ("live_transcript", "occurred_at"),
            ("live_transcript", "overlay_at"),
            ("live_transcript", "received_at"),
        )
    if event.startswith("browser_"):
        return (
            ("card", "page_painted_at_wall"),
            ("card", "page_observed_at_wall"),
            ("observer_observed_at_wall",),
        )
    return ()


def nested_get(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def parse_iso_wall_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def find_codex_tui_precondition(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    clean = strip_ansi(text)
    for pattern, reason in CODEX_TUI_PRECONDITION_PATTERNS:
        match = pattern.search(clean)
        if not match:
            continue
        return {
            "reason": reason,
            "message": match.group(0),
            "hook_count": int_or_none(match.groupdict().get("count")),
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
    for event in data.get("runtime_observations") or []:
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
        "observed_at_ms": None,
        "received_at_ms": None,
    }
    fallback: dict[str, Any] | None = None
    for event in data.get("runtime_observations") or []:
        if event.get("kind") != "terminal_signal":
            continue
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        payload_source = str(payload.get("terminal_source") or "").strip() if isinstance(payload, dict) else ""
        event_source = str(event.get("source") or "").strip()
        candidate = _terminal_details_from_event(event, payload if isinstance(payload, dict) else {})
        if fallback is None:
            fallback = candidate
        preferred_source = str(details.get("source") or "").strip()
        if preferred_source and preferred_source not in {event_source, payload_source}:
            continue
        _merge_terminal_details(details, candidate)
        return details
    if fallback is not None:
        _merge_terminal_details(details, fallback)
    return details


def _terminal_details_from_event(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    occurred_at = parse_db_timestamp(event.get("occurred_at"))
    received_at = parse_db_timestamp(event.get("received_at"))
    details: dict[str, Any] = {
        "source": str(event.get("source") or "").strip() or None,
        "state": str(payload.get("terminal_state") or "").strip() or None,
        "reason": str(payload.get("terminal_reason") or "").strip() or None,
        "ingest_lag_ms": None,
        "observed_at_ms": int(occurred_at.timestamp() * 1000) if occurred_at is not None else None,
        "received_at_ms": int(received_at.timestamp() * 1000) if received_at is not None else None,
    }
    payload_source = str(payload.get("terminal_source") or "").strip()
    if payload_source:
        details["source"] = payload_source
    if occurred_at is not None and received_at is not None:
        details["ingest_lag_ms"] = int((received_at - occurred_at).total_seconds() * 1000)
    return details


def _merge_terminal_details(details: dict[str, Any], candidate: dict[str, Any]) -> None:
    for key in ("state", "reason", "source", "ingest_lag_ms", "observed_at_ms", "received_at_ms"):
        if details.get(key) is None and candidate.get(key) is not None:
            details[key] = candidate[key]


def transcript_ingest_details(data: dict[str, Any], remote_clock_skew_ms: int | None) -> dict[str, Any]:
    details = {
        "ingest_lag_ms": None,
        "skew_adjusted_lag_ms": None,
    }
    for event in data.get("runtime_observations") or []:
        if event.get("kind") != "progress_signal":
            continue
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        if not isinstance(payload, dict) or payload.get("progress_kind") != "transcript_append":
            continue
        occurred_at = parse_db_timestamp(event.get("occurred_at"))
        received_at = parse_db_timestamp(event.get("received_at"))
        if occurred_at is None or received_at is None:
            return details
        lag_ms = int((received_at - occurred_at).total_seconds() * 1000)
        details["ingest_lag_ms"] = lag_ms
        if remote_clock_skew_ms is not None:
            details["skew_adjusted_lag_ms"] = lag_ms - remote_clock_skew_ms
        return details
    return details


def bridge_live_details(
    data: dict[str, Any],
    nonce: str,
    remote_clock_skew_ms: int | None,
) -> dict[str, Any]:
    live_events: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for event in data.get("runtime_observations") or []:
        if event.get("source") != "codex_bridge_live":
            continue
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        if not isinstance(payload, dict) or payload.get("progress_kind") != "bridge_live_transcript_delta":
            continue
        live_events.append((int_or_none(event.get("id")) or 0, event, payload))

    assembled = ""
    for _id, event, payload in sorted(live_events, key=lambda item: item[0]):
        fragment = str(payload.get("preview_text") or payload.get("delta") or "")
        if payload.get("preview_text"):
            assembled = fragment
        else:
            assembled += fragment
        if nonce not in assembled:
            continue

        occurred_at = parse_db_timestamp(event.get("occurred_at"))
        received_at = parse_db_timestamp(event.get("received_at"))
        details: dict[str, Any] = {
            "method": payload.get("method"),
            "delta_count": len(live_events),
        }
        if occurred_at is not None and received_at is not None:
            lag_ms = int((received_at - occurred_at).total_seconds() * 1000)
            details["ingest_lag_ms"] = lag_ms
            if remote_clock_skew_ms is not None:
                details["skew_adjusted_lag_ms"] = lag_ms - remote_clock_skew_ms
        return details
    return {}


def ship_trace_details(data: dict[str, Any], remote_clock_skew_ms: int | None) -> dict[str, Any]:
    for event in data.get("runtime_observations") or []:
        if event.get("source") != "agents_ingest_trace":
            continue
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        if not isinstance(payload, dict) or payload.get("progress_kind") != "ship_pipeline_trace":
            continue
        ship_trace = payload.get("ship_trace") or {}
        server_trace = payload.get("server_trace") or {}
        if not isinstance(ship_trace, dict) or not isinstance(server_trace, dict):
            continue
        details: dict[str, Any] = {}
        if isinstance(ship_trace.get("observation_source"), str):
            details["observation_source"] = ship_trace["observation_source"]
        if isinstance(ship_trace.get("wake_reason"), str):
            details["wake_reason"] = ship_trace["wake_reason"]
        for key in (
            "observation_to_enqueue_ms",
            "observation_to_wake_ms",
            "wake_to_enqueue_ms",
            "enqueue_to_job_ms",
            "observed_to_job_ms",
            "prepare_ms",
            "prepare_open_db_ms",
            "prepare_binding_wait_ms",
            "prepare_parse_ms",
            "job_to_http_ms",
        ):
            if isinstance(ship_trace.get(key), int | float):
                details[key] = int(ship_trace[key])
        if isinstance(server_trace.get("store_write_ms"), int | float):
            details["store_write_ms"] = int(server_trace["store_write_ms"])

        occurred_at = transcript_occurred_at(data)
        job_started_at_ms = int_or_none(ship_trace.get("job_started_at_ms"))
        if occurred_at is not None and job_started_at_ms is not None:
            occurred_ms = int(occurred_at.timestamp() * 1000)
            details["append_to_job_ms"] = job_started_at_ms - occurred_ms

        http_send_started_at_ms = int_or_none(ship_trace.get("http_send_started_at_ms"))
        handler_entered_at_ms = int_or_none(server_trace.get("handler_entered_at_ms"))
        if (
            http_send_started_at_ms is not None
            and handler_entered_at_ms is not None
            and remote_clock_skew_ms is not None
        ):
            details["http_to_handler_ms"] = handler_entered_at_ms - (
                http_send_started_at_ms + remote_clock_skew_ms
            )
        return details
    return {}


def transcript_occurred_at(data: dict[str, Any]) -> datetime | None:
    for event in data.get("runtime_observations") or []:
        if event.get("kind") != "progress_signal":
            continue
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        if isinstance(payload, dict) and payload.get("progress_kind") == "transcript_append":
            return parse_db_timestamp(event.get("occurred_at"))
    return None


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)


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
        "runtime_observations": (data.get("runtime_observations") or [])[:5],
    }


def compact_timeline(data: dict[str, Any]) -> dict[str, Any]:
    detail = data.get("detail") or {}
    matches = data.get("matches") or []
    return {
        "detail_status": data.get("detail_status"),
        "detail_request_ms": data.get("detail_request_ms"),
        "listing_status": data.get("listing_status"),
        "listing_request_ms": data.get("listing_request_ms"),
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
                "transcript_preview",
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
                    "transcript_preview": (card.get("head") or {}).get("transcript_preview"),
                },
            }
            for card in matches[:3]
        ],
    }


def timeline_has_card(data: dict[str, Any]) -> bool:
    return data.get("detail_status") == 200 and bool(data.get("matches"))


def timeline_transcript_preview_contains(data: dict[str, Any], nonce: str) -> bool:
    for preview in timeline_transcript_previews(data):
        text = str(preview.get("text") or preview.get("preview") or "")
        if nonce in text:
            return True
    return False


def timeline_transcript_previews(data: dict[str, Any]) -> list[dict[str, Any]]:
    transcripts: list[dict[str, Any]] = []
    detail = data.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("transcript_preview"), dict):
        transcripts.append(detail["transcript_preview"])
    for card in data.get("matches") or []:
        if not isinstance(card, dict):
            continue
        for key in ("head", "detail", "root"):
            value = card.get(key)
            if isinstance(value, dict) and isinstance(value.get("transcript_preview"), dict):
                transcripts.append(value["transcript_preview"])
    return transcripts


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


class CodexAppServerProbe:
    def __init__(self, *, cwd: Path, timeout: float = 8) -> None:
        self.cwd = cwd
        self.timeout = timeout
        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1

    def __enter__(self) -> "CodexAppServerProbe":
        self.proc = subprocess.Popen(
            ["codex", "app-server", "--enable", "hooks", "--listen", "stdio://"],
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "longhouse_managed_profiler",
                    "title": "Longhouse Managed Profiler",
                    "version": "0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized", {})
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.proc is None:
            return
        terminate_process(self.proc)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            proc = self._proc()
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.2)
            for stream in ready:
                line = stream.readline()
                if not line:
                    continue
                if stream is proc.stderr:
                    continue
                message = safe_json_loads(line)
                if not isinstance(message, dict):
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        raise TimeoutError(method)

    def longhouse_hooks(self) -> list[dict[str, Any]]:
        result = self.request("hooks/list", {"cwds": [str(ROOT)]})
        hooks: list[dict[str, Any]] = []
        for entry in result.get("data") or []:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if isinstance(hook, dict) and is_longhouse_codex_hook_candidate(hook):
                    hooks.append(hook)
        return hooks

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc()
        if proc.stdin is None:
            raise RuntimeError("codex app-server stdin unavailable")
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def _proc(self) -> subprocess.Popen[str]:
        if self.proc is None:
            raise RuntimeError("codex app-server probe not started")
        return self.proc


def is_longhouse_codex_hook_candidate(hook: dict[str, Any]) -> bool:
    return "longhouse-codex-hook.sh" in str(hook.get("command") or "")


def is_expected_longhouse_codex_hook(hook: dict[str, Any]) -> bool:
    return (
        str(hook.get("command") or "") == str(CODEX_LONGHOUSE_HOOK_SCRIPT)
        and str(hook.get("sourcePath") or "") == str(CODEX_HOOKS_JSON)
    )


def summarize_codex_hook_probe(result: dict[str, Any]) -> dict[str, Any]:
    def compact(hook: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": hook.get("key"),
            "eventName": hook.get("eventName"),
            "sourcePath": hook.get("sourcePath"),
            "enabled": hook.get("enabled"),
            "isManaged": hook.get("isManaged"),
            "trustStatus": hook.get("trustStatus"),
            "expectedLonghouseHook": is_expected_longhouse_codex_hook(hook),
        }

    before = [compact(hook) for hook in result.get("before") or [] if isinstance(hook, dict)]
    after = [compact(hook) for hook in result.get("after") or [] if isinstance(hook, dict)]
    return {
        "trusted_requested": result.get("trusted_requested"),
        "trusted_written": result.get("trusted_written"),
        "write_status": result.get("write_status"),
        "before": before,
        "after": after,
    }


def call_or_error(fn):
    try:
        return fn()
    except subprocess.TimeoutExpired as exc:
        return {"error": f"timeout after {exc.timeout}s", "cmd": redact_cmd(list(exc.cmd)) if isinstance(exc.cmd, list) else str(exc.cmd)}
    except Exception as exc:
        return {"error": str(exc)}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-sla-cases",
        action="store_true",
        help="Print the checked-in SLA case inventory and exit.",
    )
    parser.add_argument(
        "--profile",
        choices=["baseline", "cold-timeline", "warm-live"],
        default="baseline",
        help="Profiler scenario to run. warm-live measures an already-open timeline; cold-timeline opens the browser after session truth exists.",
    )
    parser.add_argument("--provider", choices=["claude", "codex"], default="codex")
    parser.add_argument("--ownership", choices=["managed", "unmanaged", "all"], default="all")
    parser.add_argument("--subdomain", default=os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN", "demo"))
    parser.add_argument("--container")
    parser.add_argument("--ssh-target", default="runtime-host")
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--name-prefix", default="lh-probe")
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--sla-case",
        help="SLA matrix case id this run measures. Defaults from --profile when the mapping is unambiguous.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Run the selected profile N times and write aggregate batch metrics.",
    )
    parser.add_argument(
        "--profile-class",
        choices=["cold_timeline", "warm_realtime", "durable_archive", "honest_degradation", "fidelity"],
        default=None,
        help="Observation profile class metadata. Narrow --profile modes will map to this in later slices.",
    )
    parser.add_argument(
        "--browser-ui-base-url",
        help="Hosted browser UI origin to profile. Defaults to https://<subdomain>.longhouse.ai.",
    )
    parser.add_argument(
        "--skip-browser-ui",
        action="store_true",
        help="Skip the Playwright browser layer and keep the profiler to HTTP/SSE/DB observers.",
    )
    parser.add_argument("--skip-managed", action="store_true")
    parser.add_argument("--skip-unmanaged", action="store_true")
    parser.add_argument(
        "--trust-longhouse-codex-hooks",
        action="store_true",
        help=(
            "Before managed runs, trust only the Longhouse hooks installed in "
            "~/.codex/hooks.json using Codex app-server's hooks/list and config/batchWrite APIs."
        ),
    )
    parser.add_argument(
        "--codex-model",
        help=(
            "Optional model override for the profiler's attached Codex TUI. "
            "Use this to keep propagation probes deterministic without changing the user's normal Codex config."
        ),
    )
    parser.add_argument(
        "--codex-effort",
        choices=["low", "medium", "high", "xhigh"],
        help=(
            "Optional model_reasoning_effort override for the profiler's attached Codex TUI. "
            "Useful for measuring Longhouse propagation separately from provider thinking latency."
        ),
    )
    return parser.parse_args(argv)


def normalize_args(args: argparse.Namespace) -> None:
    if args.profile in {"cold-timeline", "warm-live"}:
        if args.skip_browser_ui:
            raise SystemExit(f"--profile {args.profile} requires the browser UI observer")
        args.ownership = "managed"
        args.skip_unmanaged = True


def run_single(args: argparse.Namespace) -> tuple[int, Path]:
    normalize_args(args)
    profiler = Profiler(args)
    profiler.observe(
        case_id="run",
        provider=args.provider,
        ownership=args.ownership,
        source="harness",
        event="run_started",
        payload={
            "output_dir": str(profiler.output_dir),
            "project": args.project,
            "subdomain": args.subdomain,
            "container": profiler.container,
            "profile": args.profile,
            "browser_ui_base_url": profiler.browser_ui_base_url,
            "browser_ui_enabled": not args.skip_browser_ui,
            "profile_class": profiler.profile_class,
            "sla_case_id": profiler.sla_case.get("id") if profiler.sla_case else None,
            "sla_status": profiler.sla_case.get("status") if profiler.sla_case else None,
            "sla_manifest": str(DEFAULT_MANIFEST_PATH),
            "sla_manifest_summary": manifest_summary(sla_manifest()),
        },
    )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        if args.ownership in {"managed", "all"} and not args.skip_managed and args.provider == "codex":
            results.append(profiler.run_managed_codex())
        elif args.ownership in {"managed", "all"} and not args.skip_managed and args.provider == "claude":
            results.append(profiler.run_managed_claude())
    except Exception as exc:
        errors.append(f"managed {args.provider} failed: {exc}")
        profiler.observe(
            case_id="B1" if args.provider == "codex" else "C1",
            provider=args.provider,
            ownership="managed",
            source="harness",
            event="mismatch_detected",
            payload={"error": str(exc)},
        )
    try:
        if args.ownership in {"unmanaged", "all"} and not args.skip_unmanaged and args.provider == "codex":
            results.append(profiler.run_unmanaged_codex())
        elif args.ownership in {"unmanaged", "all"} and not args.skip_unmanaged:
            raise RuntimeError(f"unmanaged {args.provider} profiling is not implemented")
    except Exception as exc:
        errors.append(f"unmanaged {args.provider} failed: {exc}")
        profiler.observe(
            case_id="A1",
            provider=args.provider,
            ownership="unmanaged",
            source="harness",
            event="mismatch_detected",
            payload={"error": str(exc)},
        )
    metrics = profiler.write_summary(results, errors)
    print(profiler.summary_path)
    return single_exit_code(errors=errors, metrics=metrics, sla_status=(profiler.sla_case or {}).get("status")), profiler.summary_path


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100) * len(ordered)) - 1))
    return ordered[index]


def aggregate_batch_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key in BATCH_METRIC_KEYS:
        values = [value for case in cases if isinstance((value := case.get(key)), int)]
        metrics[key] = {
            "count": len(values),
            "min": min(values) if values else None,
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "max": max(values) if values else None,
            "target": target_for_metric(key),
        }
    return metrics


def summarize_batch_verdicts(child_runs: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for run in child_runs:
        verdict = run.get("verdict") or "error"
        counts[verdict] = counts.get(verdict, 0) + 1

    batch_verdict = "pass"
    max_severity = -1
    for verdict in counts:
        severity = BATCH_VERDICT_SEVERITY.get(verdict, BATCH_VERDICT_SEVERITY["error"])
        if severity > max_severity:
            batch_verdict = verdict
            max_severity = severity

    return {
        "batch_verdict": batch_verdict,
        "verdict_counts": counts,
    }


def batch_exit_code(*, child_runs: list[dict[str, Any]], sla_status: str | None) -> int:
    if any(run.get("exit_code") == 1 for run in child_runs):
        return 1
    saw_infra = any(run.get("exit_code") == 2 for run in child_runs)
    if sla_status == "required":
        for run in child_runs:
            verdict = run.get("verdict") or "error"
            if verdict in BATCH_REQUIRED_FAIL_VERDICTS:
                return 1
            if verdict in BATCH_REQUIRED_INFRA_VERDICTS:
                saw_infra = True
    if saw_infra:
        return 2
    return 0


def single_exit_code(*, errors: list[str], metrics: list[dict[str, Any]], sla_status: str | None) -> int:
    if errors:
        if errors_contaminated(errors):
            return 2
        return 1
    if sla_status == "required":
        saw_infra = False
        for case in metrics:
            verdict = case.get("verdict") or "error"
            if verdict in BATCH_REQUIRED_FAIL_VERDICTS:
                return 1
            if verdict in BATCH_REQUIRED_INFRA_VERDICTS:
                saw_infra = True
        if saw_infra:
            return 2
    return 0


def errors_contaminated(errors: list[str]) -> bool:
    text = "\n".join(errors)
    return any(marker in text for marker in TRANSPORT_FAILURE_PATTERNS)


def target_for_metric(key: str) -> int | None:
    manifest = sla_manifest()
    if metric_is_diagnostic(manifest, key):
        return None
    return metric_target_ms(manifest, key)


def write_batch_summary(
    *,
    batch_dir: Path,
    batch_id: str,
    child_runs: list[dict[str, Any]],
    aggregate: dict[str, Any],
) -> Path:
    summary_path = batch_dir / "summary.md"
    rows = [
        "# Managed Session Propagation Batch",
        "",
        f"- Batch ID: `{batch_id}`",
        f"- Runs: {len(child_runs)}",
        f"- Batch verdict: `{aggregate.get('batch_verdict') or 'unknown'}`",
        f"- Generated: `{utc_now()}`",
        "",
        "## Verdicts",
        "",
        "| Verdict | Count |",
        "| --- | ---: |",
    ]
    for verdict, count in sorted((aggregate.get("verdict_counts") or {}).items()):
        rows.append(f"| {verdict} | {count} |")
    rows.extend(
        [
            "",
            "## Metrics",
            "",
            "| Metric | Count | Min | P50 | P95 | Max | Target |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for key in BATCH_METRIC_KEYS:
        item = aggregate.get(key) or {}
        rows.append(
            "| "
            + " | ".join(
                [
                    f"`{key}`",
                    str(item.get("count") or 0),
                    format_optional_ms(item.get("min")),
                    format_optional_ms(item.get("p50")),
                    format_optional_ms(item.get("p95")),
                    format_optional_ms(item.get("max")),
                    format_optional_ms(item.get("target")),
                ]
            )
            + " |"
        )
    rows.extend(
        [
            "",
            "## Runs",
            "",
            "| Run | Verdict | Reason | Summary |",
            "| --- | --- | --- | --- |",
        ]
    )
    for run in child_runs:
        reason = run.get("reason") or "-"
        if isinstance(reason, dict):
            reason = reason.get("reason") or reason.get("health_state") or "preflight"
        rows.append(
            f"| `{run['run_id']}` | {run.get('verdict') or '-'} | {reason} | `{run.get('summary_path')}` |"
        )
    summary_path.write_text("\n".join(rows) + "\n")
    return summary_path


def format_optional_ms(value: Any) -> str:
    return "-" if value is None else str(value)


def batch_local_health_preflight() -> dict[str, Any]:
    completed = run_cmd(["longhouse", "local-health", "--json"], timeout=30)
    data = safe_json_loads(completed.stdout)
    if not isinstance(data, dict):
        return {
            "ok": False,
            "reason": "local_health_unparseable",
            "returncode": completed.returncode,
            "stderr": (completed.stderr or "")[-1000:],
        }
    health_state = str(data.get("health_state") or "unknown")
    transport_health = data.get("transport_health") or {}
    outbox = data.get("outbox") or {}
    outbox_oldest_age = outbox.get("oldest_age_seconds")
    outbox_stale = isinstance(outbox_oldest_age, (int, float)) and outbox_oldest_age > 10
    current_transport_ok = (
        str(transport_health.get("status") or "") != "offline"
        and transport_health.get("last_ship_result") == "ok"
        and int(transport_health.get("consecutive_failures") or 0) == 0
        and int(transport_health.get("spool_pending") or 0) == 0
        and not outbox_stale
    )
    ok = completed.returncode == 0 and not outbox_stale and (health_state == "healthy" or current_transport_ok)
    return {
        "ok": ok,
        "reason": (
            "local_outbox_stale"
            if outbox_stale
            else "local_transport_currently_unhealthy"
            if not (health_state == "healthy" or current_transport_ok)
            else None
        ),
        "health_state": health_state,
        "headline": data.get("headline"),
        "reasons": data.get("reasons") or [],
        "transport_health": transport_health,
        "outbox": outbox,
    }


def run_batch(args: argparse.Namespace) -> int:
    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1")
    sla_case = resolve_sla_case(args)
    batch_id = args.run_id or slug_now()
    batch_dir = Path(args.output_dir or DEFAULT_OUTPUT_ROOT / batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)

    child_runs: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for index in range(args.iterations):
        health = batch_local_health_preflight()
        if not health.get("ok"):
            print(
                f"[batch] aborting before iteration {index + 1}/{args.iterations}: "
                f"{health.get('reason')} health={health.get('health_state')}",
                file=sys.stderr,
                flush=True,
            )
            child_runs.append(
                {
                    "run_id": f"{batch_id}-preflight-i{index + 1:02d}",
                    "summary_path": "",
                    "metrics_path": "",
                    "exit_code": 2,
                    "verdict": "contaminated",
                    "reason": health,
                }
            )
            break
        child_args = Namespace(**vars(args))
        child_args.iterations = 1
        child_args.run_id = f"{batch_id}-i{index + 1:02d}"
        child_args.output_dir = str(batch_dir / child_args.run_id)
        print(
            f"[batch] starting iteration {index + 1}/{args.iterations}: {child_args.run_id}",
            file=sys.stderr,
            flush=True,
        )
        code, summary_path = run_single(child_args)
        metrics_path = Path(child_args.output_dir) / "metrics.json"
        metrics = read_json(metrics_path) or {}
        case = next(iter(metrics.get("cases") or []), {})
        if case:
            cases.append(case)
        child_runs.append(
            {
                "run_id": child_args.run_id,
                "summary_path": str(summary_path),
                "metrics_path": str(metrics_path),
                "exit_code": code,
                "verdict": case.get("verdict") if case else "error",
            }
        )
        print(
            f"[batch] completed iteration {index + 1}/{args.iterations}: "
            f"exit={code} verdict={child_runs[-1]['verdict']}",
            file=sys.stderr,
            flush=True,
        )
        if code == 2:
            child_runs[-1]["reason"] = "contaminated_child_run"
            print(
                f"[batch] stopping after contaminated iteration {index + 1}/{args.iterations}",
                file=sys.stderr,
                flush=True,
            )
            break

    aggregate = aggregate_batch_cases(cases)
    aggregate.update(summarize_batch_verdicts(child_runs))
    exit_code = batch_exit_code(child_runs=child_runs, sla_status=(sla_case or {}).get("status"))
    batch_metrics_path = batch_dir / "batch-metrics.json"
    batch_metrics_path.write_text(
        json.dumps(
            {
                "schema_version": BATCH_METRICS_SCHEMA_VERSION,
                "batch_id": batch_id,
                "generated_at": utc_now(),
                "profile": args.profile,
                "profile_class": args.profile_class or profile_class_for(args.profile),
                "sla_case_id": args.sla_case or default_sla_case_id(args.profile, args.provider),
                "iterations": args.iterations,
                "runs": child_runs,
                "aggregate": aggregate,
            },
            indent=2,
            sort_keys=True,
        )
    )
    summary_path = write_batch_summary(
        batch_dir=batch_dir,
        batch_id=batch_id,
        child_runs=child_runs,
        aggregate=aggregate,
    )
    print(summary_path)
    return exit_code


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.list_sla_cases:
        print(format_case_inventory(sla_manifest()))
        return 0
    if args.iterations > 1:
        return run_batch(args)
    code, _summary_path = run_single(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
