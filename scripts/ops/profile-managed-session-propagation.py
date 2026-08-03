#!/usr/bin/env python3
"""Profile managed-provider session propagation from local process truth to timeline truth.

This is the first implementation slice for
docs/specs/managed-session-propagation-profiler.md. Codex is the reference
driver; provider-specific drivers are added only when their native control and
transcript evidence is already proven.
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
import shutil
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
CODEX_LONGHOUSE_HOOK_SCRIPT = (
    Path.home() / ".codex" / "hooks" / "longhouse-codex-hook.sh"
)
BROWSER_UI_OBSERVER_SCRIPT = (
    ROOT / "scripts" / "ops" / "managed_profiler" / "browser_ui_observer.mjs"
)
HOSTED_CONTAINER_PREFIX = "longhouse-"
HOSTED_RUNTIME_OBSERVATION_LIMIT = 200
METRICS_SCHEMA_VERSION = 6
BATCH_METRICS_SCHEMA_VERSION = 5
PENDING_BROWSER_SESSION_ID = "__pending_browser_session__"
BATCH_METRIC_KEYS = (
    "cold_timeline_navigation_to_card_paint_ms",
    "cold_timeline_navigation_to_close_paint_ms",
    "warm_session_created_to_card_paint_ms",
    "content_durable_to_timeline_card_paint_ms",
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
    "browser_runtime_state_stream_to_paint_ms",
    "browser_runtime_state_fanout_to_paint_ms",
    "web_state_render_beacon_p50_ms",
    "ios_state_render_beacon_p50_ms",
    "close_observed_ms",
    "bridge_live_ingest_lag_ms",
    "browser_timeline_card_from_session_id_ms",
    "ship_trace_prepare_open_db_ms",
    "ship_trace_prepare_binding_wait_ms",
    "ship_trace_prepare_parse_ms",
    "waterfall_total_provider_to_first_render_ms",
    "waterfall_provider_to_engine_observed_ms",
    "waterfall_engine_observed_to_enqueued_ms",
    "waterfall_engine_enqueued_to_job_started_ms",
    "waterfall_engine_job_started_to_http_send_ms",
    "waterfall_http_send_to_server_handler_ms",
    "waterfall_server_handler_to_store_returned_ms",
    "waterfall_server_store_to_fanout_ms",
    "waterfall_server_fanout_to_client_received_ms",
    "waterfall_client_received_to_rendered_ms",
)
WATERFALL_STAGE_KEYS = (
    "provider_to_engine_observed",
    "engine_observed_to_enqueued",
    "engine_enqueued_to_job_started",
    "engine_job_started_to_http_send",
    "http_send_to_server_handler",
    "server_handler_to_store_returned",
    "server_store_to_fanout",
    "server_fanout_to_client_received",
    "client_received_to_rendered",
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
CLEAN_BATCH_VERDICTS = frozenset({"pass", "slow"})
TRANSPORT_FAILURE_PATTERNS = (
    "Request timed out connecting",
    "status of 524",
    "status=524",
    "ReadTimeout",
    "ERR_QUIC_PROTOCOL_ERROR",
    "ERR_HTTP2_PROTOCOL_ERROR",
    "ERR_HTTP_PROTOCOL_ERROR",
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
    return (
        metric_target_ms(sla_manifest(), "durable_archive_local_to_hosted_ms", 3_000)
        or 3_000
    )


def warm_session_created_target_ms() -> int:
    return (
        metric_target_ms(sla_manifest(), "warm_session_created_to_card_paint_ms", 500)
        or 500
    )


def content_promotion_target_ms() -> int:
    return (
        metric_target_ms(
            sla_manifest(), "content_durable_to_timeline_card_paint_ms", 500
        )
        or 500
    )


def managed_close_target_ms() -> int:
    return metric_target_ms(sla_manifest(), "close_observed_ms", 1_000) or 1_000


def cold_timeline_card_target_ms() -> int:
    return (
        metric_target_ms(
            sla_manifest(), "cold_timeline_navigation_to_card_paint_ms", 2_000
        )
        or 2_000
    )


def cold_timeline_close_target_ms() -> int:
    return (
        metric_target_ms(
            sla_manifest(), "cold_timeline_navigation_to_close_paint_ms", 2_000
        )
        or 2_000
    )


DEFAULT_SLA_CASE_BY_PROFILE_PROVIDER = {
    ("cold-timeline", "codex"): "managed_codex_cold_timeline_closed",
    ("warm-live", "codex"): "managed_codex_warm_live_graceful_close",
    ("warm-live", "claude"): "managed_claude_warm_live_graceful_close",
    ("warm-live", "cursor"): "managed_cursor_helm_content_promotion",
    ("warm-live", "opencode"): "managed_opencode_warm_lifecycle",
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
        raise SystemExit(
            f"--sla-case {case_id!r} provider={provider!r} does not match --provider {args.provider!r}"
        )
    if case.get("profile") not in {args.profile, "none"}:
        raise SystemExit(
            f"--sla-case {case_id!r} profile={case.get('profile')!r} does not match --profile {args.profile!r}"
        )
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


def cursor_helm_stop_already_complete(result: CommandResult) -> bool:
    """Return true when Cursor has already detached before an idempotent stop."""

    return result.returncode != 0 and "session_not_attached" in result.stderr


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
        self.browser_ui_base_url = (
            args.browser_ui_base_url or f"https://{self.subdomain}.longhouse.ai"
        )
        self.sla_case = resolve_sla_case(args)
        self.profile_class = args.profile_class or (
            self.sla_case.get("profile_class")
            if self.sla_case
            else profile_class_for(args.profile)
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
            "external_correlation_key": payload.get("external_correlation_key")
            if payload
            else None,
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
            if isinstance(item, dict)
            if str(item.get("session_id") or item.get("id") or "") == session_id
            or str(item.get("provider_session_id") or "") == session_id
        ]
        unmanaged = [
            item
            for item in data.get("unmanaged_session_bindings", [])
            if isinstance(item, dict)
            if str(item.get("session_id") or item.get("id") or "") == session_id
            or str(item.get("provider_session_id") or "") == session_id
        ]
        return {
            "managed": managed,
            "unmanaged": unmanaged,
            "summary": summarize_local_health(data),
        }

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
            database = data.get("database")
            return database if isinstance(database, dict) else data
        return self.hosted_db_direct(session_id)

    def hosted_db_direct(self, session_id: str) -> dict[str, Any] | None:
        script = r"""
import json, os, sqlite3, sys
subdomain, sid, runtime_observation_limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
path = f"/var/app-data/longhouse/{subdomain}/longhouse-live.db"
if not os.path.exists(path):
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
if table("live_session_catalog"):
    payload["live_session_catalog"] = one(
        '''SELECT session_id, provider, project, started_at, ended_at, closed_at,
                  user_messages, assistant_messages, tool_calls, transcript_revision,
                  primary_thread_id, hidden_from_default_timeline,
                  user_hidden_from_timeline, launch_actor, launch_surface,
                  origin_kind, updated_at
           FROM live_session_catalog WHERE session_id=?''',
        (sid,),
    )
if table("live_sessions"):
    payload["catalog_schema"] = "live"
    payload["live_session"] = one("SELECT * FROM live_sessions WHERE session_id=?", (sid,))
    payload["session"] = payload["live_session"]
    payload["runtime_state"] = one(
        "SELECT * FROM live_runtime_state WHERE session_id=? ORDER BY updated_at DESC LIMIT 1", (sid,)
    )
    payload["timeline_card"] = one("SELECT * FROM live_timeline_cards WHERE session_id=?", (sid,))
    payload["interactions"] = rows(
        '''SELECT id, provider, source, reply_transport, kind, status, can_respond,
                  occurred_at, last_seen_at, resolved_at, expires_at, projection_json
           FROM live_interaction_requests WHERE session_id=?
           ORDER BY occurred_at DESC LIMIT ?''',
        (sid, runtime_observation_limit),
    ) if table("live_interaction_requests") else []
    payload["event_stats"] = None
    payload["recent_events"] = []
else:
    payload["catalog_schema"] = "legacy"

if table("sessions"):
    session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "session_id" in session_columns:
        archived_session = one("SELECT * FROM sessions WHERE session_id=?", (sid,))
    elif "id" in session_columns:
        archived_session = one("SELECT * FROM sessions WHERE id=? OR provider_session_id=?", (sid, sid))
    else:
        archived_session = None
    if archived_session is not None:
        payload["archive_session"] = archived_session
        payload["session"] = archived_session
        wanted = (
            "session_id",
            "provider",
            "environment",
            "project",
            "machine_id",
            "started_at",
            "last_activity_at",
            "ended_at",
            "user_messages",
            "assistant_messages",
            "tool_calls",
            "summary_title",
            "first_user_message_preview",
            "last_visible_text_preview",
            "transcript_revision",
            "raw_state",
            "render_state",
            "user_state",
            "origin_kind",
            "hidden_from_default_timeline",
            "launch_actor",
            "launch_surface",
            "created_at",
            "updated_at",
        )
        available = [name for name in wanted if name in session_columns]
        payload["storage_session"] = one(
            f"SELECT {', '.join(available)} FROM sessions WHERE session_id=?",
            (sid,),
        )
if table("session_runtime_state") and "runtime_state" not in payload:
    payload["runtime_state"] = one("SELECT * FROM session_runtime_state WHERE session_id=? ORDER BY updated_at DESC LIMIT 1", (sid,))
if table("events") and "event_stats" not in payload:
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
    client_rows = rows("SELECT id, source, observed_at, received_at, payload_json FROM session_observations WHERE session_id=? AND source_domain='client' AND kind='client_render' ORDER BY id DESC LIMIT ?", (sid, runtime_observation_limit))
    payload["client_render_observations"] = []
    for row in client_rows:
        payload_json = row.pop("payload_json") or "{}"
        decoded = json.loads(payload_json)
        inner = decoded.get("payload") if isinstance(decoded, dict) else {}
        if not isinstance(inner, dict) or "render_kind" not in inner:
            inner = decoded if isinstance(decoded, dict) else {}
        row["payload"] = inner
        payload["client_render_observations"].append(row)
else:
    payload["client_render_observations"] = []
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

    def hosted_latency_report(self, session_id: str) -> dict[str, Any] | None:
        """Build the canonical nine-stage waterfall inside the hosted runtime.

        Running the read-only report in the tenant container avoids browser-auth
        concerns while still exercising the exact deployed report code and DB.
        """

        script = r"""
import json, sys
from uuid import UUID
from zerg.database import get_session_factory
from zerg.services.realtime_propagation import build_realtime_propagation_session_report

db = get_session_factory()()
try:
    report = build_realtime_propagation_session_report(
        db,
        session_id=UUID(sys.argv[1]),
        event_limit=100,
    )
    print(json.dumps(report.model_dump(mode="json") if report else None))
finally:
    db.close()
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
                session_id,
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = safe_json_loads(proc.stdout)
        return data if isinstance(data, dict) else None

    def hosted_recent_cursor_sessions(self, since_epoch: float) -> list[dict[str, Any]]:
        """Find Cursor sessions created during this launch before local state exists.

        Cursor's first turn is supplied on the native Helm launch command. The
        provider can therefore create and archive content before the local Helm
        state file exposes Longhouse's session UUID. The live catalog is the
        earliest durable hosted signal and lets the profiler establish the
        empty-shell boundary without guessing from a late state-file read.
        """

        script = r"""
import json, os, sqlite3, sys
from datetime import datetime, timezone

subdomain, cutoff_epoch = sys.argv[1], float(sys.argv[2])
path = f"/var/app-data/longhouse/{subdomain}/longhouse-live.db"
if not os.path.exists(path):
    path = f"/var/app-data/longhouse/{subdomain}/longhouse.db"
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

def epoch(value):
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

rows = []
table = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_session_catalog'"
).fetchone()
if table:
    rows = [
        dict(row)
        for row in conn.execute(
            '''SELECT session_id, provider, project, cwd, started_at, ended_at,
                      user_messages, assistant_messages, tool_calls,
                      hidden_from_default_timeline, updated_at
               FROM live_session_catalog
               WHERE provider=? AND project=?
               ORDER BY started_at DESC LIMIT 20''',
            ("cursor", "managed-local"),
        ).fetchall()
    ]
result = []
for row in rows:
    started_epoch = epoch(row.get("started_at"))
    if started_epoch is None or started_epoch < cutoff_epoch:
        continue
    row["started_at_epoch"] = started_epoch
    result.append(row)
print(json.dumps(result, default=str))
"""
        proc = subprocess.run(
            [
                "ssh",
                self.args.ssh_target,
                "python3",
                "-",
                self.subdomain,
                str(since_epoch),
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        data = safe_json_loads(proc.stdout)
        return (
            [row for row in data if isinstance(row, dict)]
            if isinstance(data, list)
            else []
        )

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

        explicit_token = os.environ.get(
            "LONGHOUSE_BROWSER_SESSION_TOKEN"
        ) or os.environ.get("LONGHOUSE_DEVICE_TOKEN")
        if explicit_token and explicit_token.strip():
            self._browser_session_cookie = explicit_token.strip()
            return self._browser_session_cookie

        machine_state_path = Path.home() / ".longhouse" / "machine" / "state.json"
        machine_state = read_json(machine_state_path)
        configured_url = str((machine_state or {}).get("runtime_url") or "").rstrip("/")
        target_url = self.browser_ui_base_url.rstrip("/")
        if configured_url == target_url:
            device_token_path = machine_state_path.parent / "device-token"
            try:
                device_token = device_token_path.read_text().strip()
            except OSError:
                device_token = ""
            if device_token:
                self._browser_session_cookie = device_token
                return self._browser_session_cookie

        script = r"""
import os

# Hosted runtime API processes deliberately do not open the catalog-owned live
# SQLite store through the retired default session factory.  This isolated
# profiler subprocess needs the live store only to mint a browser token for
# the real user-facing surfaces.
os.environ["DATABASE_URL"] = "sqlite:////data/longhouse-live.db"
os.environ["TESTING"] = "1"

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
        timeout: float = 90,
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
timeout_seconds = float(sys.argv[5])
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
    timeout = httpx.Timeout(timeout_seconds + 5, connect=3.0, read=timeout_seconds + 5)
    with httpx.stream(
        "GET",
        f"http://127.0.0.1:8000/api/timeline/sessions/{sid}/workspace/stream",
        params=params,
        headers=headers,
        timeout=timeout,
    ) as response:
        if response.status_code != 200:
            body = response.read().decode("utf-8", errors="replace")
            emit("error", error=f"status={response.status_code} body={body[:500]}")
            raise SystemExit(0)
        emit("ready")
        deadline = time.monotonic() + timeout_seconds
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
                str(timeout),
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
    if not isinstance(session, dict):
        return None
    candidates = [
        session.get("id"),
        session.get("thread_id"),
        session.get("session_id"),
    ]
    for child_key in ("head", "current", "latest"):
        child = session.get(child_key)
        if isinstance(child, dict):
            candidates.extend(
                [child.get("id"), child.get("thread_id"), child.get("session_id")]
            )
    if sid in {str(candidate) for candidate in candidates if candidate is not None}:
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
        if self.args.browser_transport == "disable-quic":
            env["LONGHOUSE_PROFILER_DISABLE_QUIC"] = "1"

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
            "transport_mode": "browser_transport_mode",
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
            "detail_workspace_request": "browser_detail_workspace_request",
            "detail_workspace_response": "browser_detail_workspace_response",
            "detail_workspace_failed": "browser_detail_workspace_failed",
            "detail_workspace_root_ready": "browser_detail_workspace_root_ready",
            "detail_workspace_stream_ready": "browser_detail_workspace_stream_ready",
            "client_render_beacon_request": "browser_client_render_beacon_request",
            "client_render_beacon_response": "browser_client_render_beacon_response",
            "client_render_beacon_failed": "browser_client_render_beacon_failed",
            "client_render_beacon_payload": "browser_client_render_beacon_payload",
            "timeline_page_closed_after_card": "browser_timeline_page_closed_after_card",
            "timeline_stream_connected": "browser_timeline_stream_connected",
            "timeline_stream_heartbeat": "browser_timeline_stream_heartbeat",
            "timeline_stream_session_upsert": "browser_timeline_stream_session_upsert",
            "timeline_stream_session_remove": "browser_timeline_stream_session_remove",
            "timeline_stream_workspace_connected": "browser_workspace_stream_connected",
            "timeline_stream_workspace_changed": "browser_workspace_stream_changed",
            "timeline_stream_workspace_preview_changed": "browser_workspace_preview_stream_changed",
            "runtime_state_painted": "browser_runtime_state_painted",
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
            "detail_workspace_root_ready_timeout": "browser_detail_workspace_root_ready_timeout",
            "runtime_state_timeout": "browser_runtime_state_painted_timeout",
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
            if event in {
                "browser_close_card_painted",
                "browser_cold_close_card_painted",
            }:
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
                payload={
                    "returncode": proc.returncode,
                    "stderr": stderr[-1000:],
                    "script": str(script_path),
                },
            )

    def start_timeline_live_poll(
        self,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
        timeout: float = 90,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: self.poll_timeline_transcript_preview(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                timeout=timeout,
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
        timeout: float = 90,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: self.stream_timeline_transcript_preview_sse(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                timeout=timeout,
            ),
            name=f"timeline-live-sse-{session_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def observe_content_promotion_baseline(
        self,
        session_id: str,
        *,
        case_id: str,
        ownership: str,
        hosted: dict[str, Any] | None = None,
        observation_interval_ms: int = 0,
    ) -> bool:
        data = hosted if hosted is not None else self.hosted_db_direct(session_id)
        if data is not None and hosted_empty_shell(data):
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="hosted_db",
                event="empty_shell_observed",
                session_id=session_id,
                payload={
                    "observation_interval_ms": observation_interval_ms,
                    **compact_hosted(data),
                },
            )
            return True
        self.observe(
            case_id=case_id,
            provider=self.args.provider,
            ownership=ownership,
            source="hosted_db",
            event="empty_shell_observation_missing",
            session_id=session_id,
            payload={
                "observation_interval_ms": observation_interval_ms,
                **compact_hosted(data or {}),
            },
        )
        return False

    def poll_content_promotion(
        self,
        session_id: str,
        *,
        case_id: str,
        ownership: str,
        timeout: float = 90,
        interval: float = 0.1,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        baseline_seen = (
            self.event_observed_at_ms(case_id, session_id, "empty_shell_observed")
            is not None
        )
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.hosted_db_direct(session_id)
            if last is not None and hosted_empty_shell(last):
                if not baseline_seen:
                    self.observe(
                        case_id=case_id,
                        provider=self.args.provider,
                        ownership=ownership,
                        source="hosted_db",
                        event="empty_shell_observed",
                        session_id=session_id,
                        payload={
                            "observation_interval_ms": int(interval * 1000),
                            **compact_hosted(last),
                        },
                    )
                    baseline_seen = True
            if baseline_seen and last is not None and hosted_content_published(last):
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_db",
                    event="content_durable_published",
                    session_id=session_id,
                    payload={
                        "baseline_proven": True,
                        "observation_interval_ms": int(interval * 1000),
                        **compact_hosted(last),
                    },
                )
                return last
            time.sleep(interval)
        self.observe(
            case_id=case_id,
            provider=self.args.provider,
            ownership=ownership,
            source="hosted_db",
            event="content_durable_published_timeout",
            session_id=session_id,
            payload={
                "baseline_proven": baseline_seen,
                "observation_interval_ms": int(interval * 1000),
                **compact_hosted(last or {}),
            },
        )
        return last

    def start_content_promotion_poll(
        self,
        session_id: str,
        *,
        case_id: str,
        ownership: str,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: self.poll_content_promotion(
                session_id,
                case_id=case_id,
                ownership=ownership,
            ),
            name=f"content-promotion-poll-{session_id}",
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
            "--url",
            self.browser_ui_base_url,
            "--no-attach",
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
        ws_url = None
        if session_id:
            ws_url = parse_remote_target(launch.stdout)
            if not ws_url:
                ws_url = self.wait_bridge_ws_url(
                    session_id,
                    case_id=case_id,
                    ownership=ownership,
                )
        if not session_id or not ws_url:
            raise RuntimeError(
                f"managed launch did not return session/ws url: {launch.short()}"
            )
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
        if self.args.profile != "warm-live":
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

        tui: subprocess.Popen[str] | None = None
        precondition: dict[str, Any] | None = None
        if self.args.profile != "warm-live":
            tui_log = self.output_dir / f"{session_id}-managed-tui.log"
            tui_cmd = [
                "/opt/homebrew/bin/codex",
                "-c",
                "check_for_update_on_startup=false",
            ]
            if self.args.codex_effort:
                tui_cmd.extend(
                    ["-c", f"model_reasoning_effort={self.args.codex_effort}"]
                )
            if self.args.codex_model:
                tui_cmd.extend(["--model", self.args.codex_model])
            tui_cmd.extend(
                ["--enable", "tui_app_server", "--remote", ws_url, "--no-alt-screen"]
            )
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
        state = self.wait_bridge_thread(
            session_id, case_id=case_id, ownership=ownership
        )
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
        if tui is not None:
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
                [
                    "longhouse-engine",
                    "codex-bridge",
                    "stop",
                    "--session-id",
                    session_id,
                    "--force",
                ],
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
        content_promotion_poll = None
        if self.args.profile != "cold-timeline":
            timeline_live_poll = self.start_timeline_live_poll(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                timeout=15,
            )
            timeline_live_sse = self.start_timeline_live_sse(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                timeout=15,
            )
        content_promotion_case = bool(
            self.sla_case
            and "content_durable_to_timeline_card_paint_ms"
            in set(self.sla_case.get("metrics") or [])
        )
        if self.args.profile == "warm-live":
            browser_session_ready = self.wait_for_observation(
                case_id,
                session_id,
                "browser_session_id_received",
                timeout=15,
            )
            browser_workspace_ready = (
                self.event_observed_at_ms(
                    case_id,
                    session_id,
                    "browser_detail_workspace_stream_ready",
                )
                is not None
                if content_promotion_case
                else self.wait_for_observation(
                    case_id,
                    session_id,
                    "browser_detail_workspace_stream_ready",
                    timeout=45,
                )
            )
            sse_ready = self.wait_for_observation(
                case_id,
                session_id,
                "timeline_transcript_preview_sse_ready",
                timeout=10,
            )
            warm_ready = (
                browser_session_ready
                and sse_ready
                and (browser_workspace_ready or content_promotion_case)
            )
            if warm_ready:
                self.observe(
                    case_id=case_id,
                    provider="codex",
                    ownership=ownership,
                    source="harness",
                    event="warm_ready_at",
                    session_id=session_id,
                    payload={
                        "browser_session_ready": True,
                        "browser_workspace_stream_ready": browser_workspace_ready,
                        "browser_workspace_stream_required": not content_promotion_case,
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
                        "browser_session_ready": browser_session_ready,
                        "browser_workspace_stream_ready": browser_workspace_ready,
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
                    [
                        "longhouse-engine",
                        "codex-bridge",
                        "stop",
                        "--session-id",
                        session_id,
                        "--force",
                    ],
                    case_id=case_id,
                    ownership=ownership,
                    event_prefix="shutdown",
                    timeout=60,
                    session_id=session_id,
                )
                terminate_process(tui)
                if browser_ui is not None:
                    browser_ui.join(timeout=150)
                self.write_snapshot(
                    case_id, ownership, session_id, "warm_ready_timeout"
                )
                return {
                    "case_id": case_id,
                    "session_id": session_id,
                    "nonce": nonce,
                    "thread_id": thread_id,
                    "thread_path": str(thread_path) if thread_path else None,
                    "precondition": {
                        "reason": "warm_live_precondition_timeout",
                        "browser_session_ready": browser_session_ready,
                        "browser_workspace_stream_ready": browser_workspace_ready,
                        "timeline_sse_ready": sse_ready,
                    },
                }
            self.observe_content_promotion_baseline(
                session_id,
                case_id=case_id,
                ownership=ownership,
            )
            content_promotion_poll = self.start_content_promotion_poll(
                session_id,
                case_id=case_id,
                ownership=ownership,
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
        if content_promotion_poll is not None:
            content_promotion_poll.join(timeout=95)
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
            [
                "longhouse-engine",
                "codex-bridge",
                "stop",
                "--session-id",
                session_id,
                "--force",
            ],
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

    def run_managed_cursor(self) -> dict[str, Any]:
        """Run the native Cursor Helm path through durable content promotion."""

        from zerg.qa.cursor_helm_product_e2e import _PtyProcess
        from zerg.qa.cursor_helm_product_e2e import _state_ids
        from zerg.services.longhouse_paths import get_managed_local_dir

        case_id = "D1"
        ownership = "managed"
        nonce = f"LH_PROBE_CURSOR_MANAGED_{self.run_id}"
        workspace = Path(
            self.args.cursor_workspace
            or Path.home()
            / ".longhouse"
            / "canaries"
            / "provider-live"
            / "cursor"
            / "product-e2e"
            / "workspace"
        ).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        state_root = get_managed_local_dir("cursor-helm")
        before_ids = _state_ids(state_root)
        session: Any | None = None
        session_id: str | None = None
        browser_ui: threading.Thread | None = None
        content_promotion_poll: threading.Thread | None = None
        content_promotion_case = bool(
            self.sla_case
            and "content_durable_to_timeline_card_paint_ms"
            in set(self.sla_case.get("metrics") or [])
        )
        hosted_discovered_session_id: str | None = None
        launch_started_epoch: float | None = None
        timeline_live_poll: threading.Thread | None = None
        timeline_live_sse: threading.Thread | None = None
        timeline_close_sse: threading.Thread | None = None
        session_id_file = self.output_dir / "browser-session-id.txt"
        session_id_file.unlink(missing_ok=True)
        terminal_path = self.output_dir / "cursor-terminal.raw"

        try:
            self.browser_session_cookie()
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
                if not browser_ready or not stream_ready:
                    raise RuntimeError(
                        "warm Cursor browser observer did not reach ready state before managed launch: "
                        f"browser_ready={browser_ready} stream_ready={stream_ready}"
                    )

            longhouse = shutil.which("longhouse")
            if not longhouse:
                raise RuntimeError("installed longhouse binary is required")
            launch_cmd = [
                longhouse,
                "cursor",
                "--cwd",
                str(workspace),
                "--permission-mode",
                "remote_approve",
                "--project",
                self.project,
                "--url",
                self.browser_ui_base_url,
                "--",
                "--model",
                self.args.cursor_model,
                f"Reply with exactly {nonce}",
            ]
            launch_started_epoch = (
                time.time() + ((self.remote_clock_skew_ms or 0) / 1000.0) - 2.0
            )
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="launch_requested",
                payload={
                    "nonce": nonce,
                    "cmd": redact_cmd(launch_cmd),
                    "workspace": str(workspace),
                },
            )
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="prompt_sent_started",
                payload={"transport": "cursor_helm_launch_prompt", "nonce": nonce},
            )
            session = _PtyProcess.start(
                launch_cmd, cwd=workspace, terminal_path=terminal_path
            )
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="managed_launch_completed",
                payload={"pid": session.process.pid, "terminal": str(terminal_path)},
            )

            last_hosted_discovery = 0.0

            def discover_hosted_cursor_session() -> None:
                nonlocal \
                    hosted_discovered_session_id, \
                    session_id, \
                    content_promotion_poll
                nonlocal last_hosted_discovery
                if launch_started_epoch is None or hosted_discovered_session_id:
                    return
                now = time.monotonic()
                if now - last_hosted_discovery < 0.25:
                    return
                last_hosted_discovery = now
                candidates = self.hosted_recent_cursor_sessions(launch_started_epoch)
                workspace_candidates = [
                    row
                    for row in candidates
                    if str(row.get("cwd") or "") == str(workspace)
                ]
                candidates = workspace_candidates or candidates
                if not candidates:
                    return
                candidate = candidates[0]
                candidate_id = str(candidate.get("session_id") or "")
                if not candidate_id:
                    return
                hosted = self.hosted_db_direct(candidate_id)
                if hosted is None:
                    return
                hosted_discovered_session_id = candidate_id
                session_id = candidate_id
                session_id_file.write_text(candidate_id + "\n")
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="hosted_db",
                    event="hosted_session_discovered",
                    session_id=candidate_id,
                    payload={
                        "discovery_latency_ms": monotonic_ms()
                        - self.started_monotonic_ms,
                        "catalog_row": candidate,
                    },
                )
                if self.args.profile == "warm-live" and content_promotion_case:
                    baseline = self.observe_content_promotion_baseline(
                        candidate_id,
                        case_id=case_id,
                        ownership=ownership,
                        hosted=hosted,
                        observation_interval_ms=monotonic_ms()
                        - self.started_monotonic_ms,
                    )
                    content_promotion_poll = self.start_content_promotion_poll(
                        candidate_id,
                        case_id=case_id,
                        ownership=ownership,
                    )
                    self.observe(
                        case_id=case_id,
                        provider=self.args.provider,
                        ownership=ownership,
                        source="harness",
                        event="content_promotion_observer_started",
                        session_id=candidate_id,
                        payload={"baseline_proven": baseline},
                    )

            trust_prompt_sent = False
            trust_deadline = time.monotonic() + 15
            while time.monotonic() < trust_deadline and not trust_prompt_sent:
                discover_hosted_cursor_session()
                try:
                    terminal = terminal_path.read_text(errors="ignore")
                except OSError:
                    terminal = ""
                if (
                    "Trust this workspace" in terminal
                    and "Use arrow keys to navigate" in terminal
                ):
                    os.write(session.master_fd, b"a")
                    time.sleep(0.15)
                    os.write(session.master_fd, b"\r")
                    trust_prompt_sent = True
                    self.observe(
                        case_id=case_id,
                        provider=self.args.provider,
                        ownership=ownership,
                        source="cursor_native_hooks",
                        event="cursor_workspace_trust_accepted",
                        payload={"workspace": str(workspace)},
                    )
                    break
                if _state_ids(state_root) - before_ids:
                    break
                time.sleep(0.1)

            deadline = time.monotonic() + 60
            state: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                discover_hosted_cursor_session()
                for candidate in _state_ids(state_root) - before_ids:
                    row = read_json(state_root / f"{candidate}.json")
                    if row and row.get("ready") is True:
                        state = row
                        break
                if state is not None:
                    break
                time.sleep(0.1)
            if state is None:
                raise RuntimeError(
                    f"timed out waiting for Cursor Helm state under {state_root}"
                )
            local_session_id = str(state.get("session_id") or "")
            if not local_session_id:
                raise RuntimeError(f"Cursor Helm state has no session id: {state}")
            if (
                hosted_discovered_session_id
                and hosted_discovered_session_id != local_session_id
            ):
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="harness",
                    event="hosted_local_session_id_mismatch",
                    session_id=hosted_discovered_session_id,
                    payload={
                        "hosted_session_id": hosted_discovered_session_id,
                        "local_session_id": local_session_id,
                    },
                )
                raise RuntimeError(
                    "Cursor hosted discovery did not bind to local Helm state: "
                    f"hosted={hosted_discovered_session_id} local={local_session_id}"
                )
            session_id = local_session_id
            claim_path = state_root / "binding-probes" / f"{session_id}.json"
            claim: dict[str, Any] | None = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                claim = read_json(claim_path)
                if claim:
                    break
                time.sleep(0.1)
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="cursor_native_hooks",
                event="managed_state_observed",
                session_id=session_id,
                provider_session_id=str((claim or {}).get("conversation_uuid") or "")
                or None,
                payload={
                    "state": state,
                    "binding_claim": claim,
                    "state_root": str(state_root),
                },
            )
            session_id_file.write_text(session_id + "\n")
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="session_id_observed",
                session_id=session_id,
                provider_session_id=str((claim or {}).get("conversation_uuid") or "")
                or None,
                payload={
                    "state_root": str(state_root),
                    "binding_claim_path": str(claim_path),
                },
            )
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="prompt_sent_started",
                session_id=session_id,
                provider_session_id=str((claim or {}).get("conversation_uuid") or "")
                or None,
                payload={
                    "transport": "cursor_helm_launch_prompt",
                    "nonce": nonce,
                    "already_in_flight": True,
                },
            )

            if (
                self.args.profile not in {"cold-timeline", "warm-live"}
                and not self.args.skip_browser_ui
            ):
                browser_ui = self.start_browser_ui_observer(
                    session_id,
                    nonce,
                    case_id=case_id,
                    ownership=ownership,
                )

            if self.args.profile == "warm-live" and content_promotion_poll is None:
                self.observe_content_promotion_baseline(
                    session_id,
                    case_id=case_id,
                    ownership=ownership,
                )
                content_promotion_poll = self.start_content_promotion_poll(
                    session_id,
                    case_id=case_id,
                    ownership=ownership,
                )
            self.write_snapshot(case_id, ownership, session_id, "post_launch")

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
                browser_session_ready = self.wait_for_observation(
                    case_id,
                    session_id,
                    "browser_session_id_received",
                    timeout=15,
                )
                browser_workspace_ready = (
                    self.event_observed_at_ms(
                        case_id, session_id, "browser_detail_workspace_stream_ready"
                    )
                    is not None
                    if content_promotion_case
                    else self.wait_for_observation(
                        case_id,
                        session_id,
                        "browser_detail_workspace_stream_ready",
                        timeout=45,
                    )
                )
                sse_ready = self.wait_for_observation(
                    case_id,
                    session_id,
                    "timeline_transcript_preview_sse_ready",
                    timeout=10,
                )
                if not browser_session_ready or not sse_ready:
                    self.observe(
                        case_id=case_id,
                        provider=self.args.provider,
                        ownership=ownership,
                        source="harness",
                        event="provider_precondition_blocked",
                        session_id=session_id,
                        payload={
                            "reason": "warm_live_precondition_timeout",
                            "browser_session_ready": browser_session_ready,
                            "browser_workspace_stream_ready": browser_workspace_ready,
                            "timeline_sse_ready": sse_ready,
                        },
                    )
                else:
                    self.observe(
                        case_id=case_id,
                        provider=self.args.provider,
                        ownership=ownership,
                        source="harness",
                        event="warm_ready_at",
                        session_id=session_id,
                        payload={
                            "browser_session_ready": True,
                            "browser_workspace_stream_ready": browser_workspace_ready,
                            "browser_workspace_stream_required": not content_promotion_case,
                            "timeline_sse_ready": True,
                        },
                    )

            self.poll_local_cursor_assistant_response(
                state_root,
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                timeout=180,
            )
            if (
                self.event_observed_at_ms(
                    case_id, session_id, "assistant_response_local"
                )
                is not None
            ):
                self.poll_hosted_session(
                    session_id,
                    case_id=case_id,
                    ownership=ownership,
                    predicate=lambda data: hosted_assistant_events_contain(data, nonce),
                    event="assistant_response_hosted",
                    timeout=180,
                    interval=0.5,
                )
            if content_promotion_poll is not None:
                content_promotion_poll.join(timeout=95)
            if timeline_live_poll is not None:
                timeline_live_poll.join(timeout=95)
            if timeline_live_sse is not None:
                timeline_live_sse.join(timeout=95)
            if browser_ui is not None:
                browser_ui.join(timeout=150)
            self.write_snapshot(case_id, ownership, session_id, "post_response")

            timeline_close_sse = self.start_timeline_close_sse(
                session_id,
                case_id=case_id,
                ownership=ownership,
            )
            self.wait_for_observation(
                case_id,
                session_id,
                "timeline_close_sse_ready",
                timeout=10,
            )
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="shutdown_requested",
                session_id=session_id,
            )
            stop = self.run_observed(
                ["longhouse-engine", "cursor-helm", "stop", "--session-id", session_id],
                case_id=case_id,
                ownership=ownership,
                event_prefix="shutdown",
                timeout=60,
                session_id=session_id,
            )
            if stop.returncode != 0 and not cursor_helm_stop_already_complete(stop):
                raise RuntimeError(f"Cursor Helm stop failed: {stop.short()}")
            if cursor_helm_stop_already_complete(stop):
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="harness",
                    event="shutdown_already_detached",
                    session_id=session_id,
                    payload=stop.short(),
                )
            if session is not None:
                session.close()
                session = None
            self.poll_hosted_session(
                session_id,
                case_id=case_id,
                ownership=ownership,
                predicate=lambda data: lifecycle_closed(data),
                event="hosted_runtime_closed",
                timeout=30,
                interval=0.25,
            )
            if timeline_close_sse is not None:
                timeline_close_sse.join(timeout=12)
            self.write_snapshot(case_id, ownership, session_id, "post_shutdown")
            return {
                "case_id": case_id,
                "session_id": session_id,
                "nonce": nonce,
                "provider_session_id": str((claim or {}).get("conversation_uuid") or "")
                or None,
                "state_root": str(state_root),
                "workspace": str(workspace),
            }
        finally:
            if session_id:
                try:
                    run_cmd(
                        [
                            "longhouse-engine",
                            "cursor-helm",
                            "stop",
                            "--session-id",
                            session_id,
                        ],
                        timeout=15,
                    )
                except Exception:
                    pass
            if session is not None:
                session.close()

    def run_managed_opencode(self) -> dict[str, Any]:
        """Run a real attached OpenCode Helm turn through the shared UI path."""

        from zerg.qa.opencode_conversation_reset import _matching_session
        from zerg.qa.pty_session import ProviderPtySession
        from zerg.qa.pty_session import wait_for_terminal_quiescence

        case_id = "E1"
        ownership = "managed"
        nonce = f"LH_PROBE_OPENCODE_MANAGED_{self.run_id}"
        name = f"{self.args.name_prefix}-managed-opencode-{self.run_id}"
        workspace = Path(
            self.args.opencode_workspace
            or Path.home()
            / ".longhouse"
            / "canaries"
            / "provider-live"
            / "opencode"
            / "workspace"
        ).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        terminal_path = self.output_dir / "opencode-terminal.raw"
        database_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        state_roots = (
            Path.home() / ".claude" / "managed-local" / "opencode-server",
            Path.home()
            / ".longhouse"
            / "managed-local"
            / "opencode"
            / "bridge"
            / "sessions",
        )
        session_id_file = self.output_dir / "browser-session-id.txt"
        session_id_file.unlink(missing_ok=True)
        started_at = time.time()
        session: ProviderPtySession | None = None
        session_id: str | None = None
        browser_ui: threading.Thread | None = None

        try:
            self.browser_session_cookie()
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
                if not browser_ready or not stream_ready:
                    raise RuntimeError(
                        "warm OpenCode browser observer did not reach ready state before managed launch: "
                        f"browser_ready={browser_ready} stream_ready={stream_ready}"
                    )

            launch_cmd = [
                "longhouse",
                "opencode",
                "--cwd",
                str(workspace),
                "--project",
                self.project,
                "--name",
                name,
                "--url",
                self.browser_ui_base_url,
            ]
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="launch_requested",
                payload={
                    "nonce": nonce,
                    "cmd": redact_cmd(launch_cmd),
                    "workspace": str(workspace),
                },
            )
            session = ProviderPtySession.start(
                argv=launch_cmd,
                cwd=workspace,
                env=os.environ.copy(),
                terminal_path=terminal_path,
                thread_name="managed-opencode-profiler-terminal",
            )
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="managed_launch_completed",
                payload={"pid": session.process.pid, "terminal": str(terminal_path)},
            )

            state: dict[str, Any] | None = None
            state_path: Path | None = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if not session.alive():
                    raise RuntimeError(
                        f"managed OpenCode exited during launch ({session.process.returncode})"
                    )
                for root in state_roots:
                    for candidate in root.glob("*.json"):
                        try:
                            if candidate.stat().st_mtime < started_at - 1:
                                continue
                        except OSError:
                            continue
                        row = read_json(candidate)
                        if not row or not row.get("server_url"):
                            continue
                        if Path(str(row.get("cwd") or "")).resolve() != workspace:
                            continue
                        state = row
                        state_path = candidate
                        break
                    if state is not None:
                        break
                if state is not None:
                    break
                time.sleep(0.05)
            if state is None or state_path is None:
                raise RuntimeError(
                    "timed out waiting for managed OpenCode bridge state"
                )

            session_id = str(state.get("session_id") or state_path.stem)
            provider_session_id = str(state.get("provider_session_id") or "")
            session_id_file.write_text(session_id + "\n")
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="opencode_server_bridge",
                event="session_id_observed",
                session_id=session_id,
                provider_session_id=provider_session_id or None,
                payload={
                    "state_path": str(state_path),
                    "server_url": state.get("server_url"),
                },
            )
            self.write_snapshot(case_id, ownership, session_id, "post_launch")

            timeline_live_poll = self.start_timeline_live_poll(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                timeout=15,
            )
            timeline_live_sse = self.start_timeline_live_sse(
                session_id,
                nonce,
                case_id=case_id,
                ownership=ownership,
                timeout=15,
            )
            content_promotion_poll: threading.Thread | None = None
            if self.args.profile == "warm-live":
                browser_session_ready = self.wait_for_observation(
                    case_id,
                    session_id,
                    "browser_session_id_received",
                    timeout=20,
                )
                sse_ready = self.wait_for_observation(
                    case_id,
                    session_id,
                    "timeline_transcript_preview_sse_ready",
                    timeout=10,
                )
                if not browser_session_ready or not sse_ready:
                    raise RuntimeError(
                        "warm OpenCode observers did not bind to the managed session: "
                        f"browser_session_ready={browser_session_ready} timeline_sse_ready={sse_ready}"
                    )
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="harness",
                    event="warm_ready_at",
                    session_id=session_id,
                    payload={"browser_session_ready": True, "timeline_sse_ready": True},
                )
                self.observe_content_promotion_baseline(
                    session_id,
                    case_id=case_id,
                    ownership=ownership,
                )
                content_promotion_poll = self.start_content_promotion_poll(
                    session_id,
                    case_id=case_id,
                    ownership=ownership,
                )

            wait_for_terminal_quiescence(session, timeout=45)
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="prompt_sent_started",
                session_id=session_id,
                payload={"transport": "opencode_attached_tui", "nonce": nonce},
            )
            session.submit_line(f"Reply with exactly {nonce}")
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="prompt_sent",
                session_id=session_id,
                payload={"transport": "opencode_attached_tui", "nonce": nonce},
            )

            local_provider_session_id: str | None = None
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                local_provider_session_id = _matching_session(
                    database_path, nonce, assistant=True
                )
                if local_provider_session_id:
                    break
                if not session.alive():
                    break
                time.sleep(0.02)
            if local_provider_session_id:
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="opencode_sqlite",
                    event="assistant_response_local",
                    session_id=session_id,
                    provider_session_id=local_provider_session_id,
                    payload={"database_path": str(database_path), "nonce": nonce},
                )
                self.poll_hosted_session(
                    session_id,
                    case_id=case_id,
                    ownership=ownership,
                    predicate=lambda data: hosted_assistant_events_contain(data, nonce),
                    event="assistant_response_hosted",
                    timeout=180,
                    interval=0.25,
                )
            else:
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="opencode_sqlite",
                    event="provider_response_timeout",
                    session_id=session_id,
                    payload={"database_path": str(database_path)},
                )

            if content_promotion_poll is not None:
                content_promotion_poll.join(timeout=20)
            timeline_live_poll.join(timeout=20)
            timeline_live_sse.join(timeout=20)
            self.write_snapshot(case_id, ownership, session_id, "post_response")

            timeline_close_sse = self.start_timeline_close_sse(
                session_id,
                case_id=case_id,
                ownership=ownership,
            )
            self.wait_for_observation(
                case_id,
                session_id,
                "timeline_close_sse_ready",
                timeout=10,
            )
            self.observe(
                case_id=case_id,
                provider=self.args.provider,
                ownership=ownership,
                source="harness",
                event="shutdown_requested",
                session_id=session_id,
            )
            stop = self.run_observed(
                ["longhouse", "opencode", "stop", "--session-id", session_id],
                case_id=case_id,
                ownership=ownership,
                event_prefix="shutdown",
                timeout=30,
                session_id=session_id,
            )
            if stop.returncode != 0:
                raise RuntimeError(f"managed OpenCode stop failed: {stop.short()}")
            session.close()
            session = None
            self.poll_hosted_session(
                session_id,
                case_id=case_id,
                ownership=ownership,
                predicate=lambda data: lifecycle_closed(data),
                event="hosted_runtime_closed",
                timeout=30,
                interval=0.25,
            )
            timeline_close_sse.join(timeout=12)
            if browser_ui is not None:
                browser_ui.join(timeout=150)
            self.write_snapshot(case_id, ownership, session_id, "post_shutdown")
            return {
                "case_id": case_id,
                "session_id": session_id,
                "nonce": nonce,
                "provider_session_id": local_provider_session_id
                or provider_session_id
                or None,
                "state_path": str(state_path),
                "workspace": str(workspace),
            }
        finally:
            if session_id:
                try:
                    run_cmd(
                        ["longhouse", "opencode", "stop", "--session-id", session_id],
                        timeout=15,
                    )
                except Exception:
                    pass
            if session is not None:
                session.close()

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
            imported_count, session_id, observed_expected = (
                self.import_claude_poc_events(
                    events_path,
                    case_id=case_id,
                    ownership=ownership,
                    imported_count=imported_count,
                    session_id=session_id,
                    observed_expected=observed_expected,
                )
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
            raise RuntimeError(
                f"managed Claude POC did not report a session id: returncode={proc.returncode}"
            )
        if not observed_expected:
            raise RuntimeError(
                "managed Claude POC failed: "
                f"returncode={proc.returncode} observed_expected={observed_expected}"
            )
        if proc.returncode != 0:
            self.observe(
                case_id=case_id,
                provider="claude",
                ownership=ownership,
                source="claude_poc",
                event="provider_close_degraded",
                session_id=session_id,
                payload={"returncode": proc.returncode},
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
            row_session_id = (
                str(payload.get("session_id") or session_id or "").strip() or None
            )
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
                    payload={
                        "timestamp": timestamp,
                        "poc_event": event,
                        "observed_at_wall": wall,
                        **payload,
                    },
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
        result = self.probe_codex_longhouse_hooks(
            trust=self.args.trust_longhouse_codex_hooks
        )
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
                "write_status": (write_result or {}).get("status")
                if isinstance(write_result, dict)
                else None,
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

    def wait_bridge_thread(
        self, session_id: str, *, case_id: str, ownership: str
    ) -> dict[str, Any] | None:
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

    def wait_bridge_ws_url(
        self, session_id: str, *, case_id: str, ownership: str
    ) -> str | None:
        state_path = BRIDGE_ROOT / f"{session_id}.json"
        deadline = time.monotonic() + 30
        last = None
        while time.monotonic() < deadline:
            last = read_json(state_path)
            ws_url = str((last or {}).get("ws_url") or "").strip()
            if ws_url:
                return ws_url
            time.sleep(0.25)
        self.observe(
            case_id=case_id,
            provider="codex",
            ownership=ownership,
            source="codex_bridge_state",
            event="managed_ws_url_timeout",
            session_id=session_id,
            payload={"state_path": str(state_path), "last": last},
        )
        return None

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

    def poll_local_cursor_assistant_response(
        self,
        hook_root: Path,
        session_id: str,
        nonce: str,
        *,
        case_id: str,
        ownership: str,
        timeout: float = 180,
        interval: float = 0.1,
    ) -> dict[str, Any] | None:
        """Observe the native Cursor afterAgentResponse hook for one marker."""

        from zerg.qa.cursor_helm_product_e2e import _hook_rows

        deadline = time.monotonic() + timeout
        last_count = 0
        while time.monotonic() < deadline:
            rows = _hook_rows(hook_root, session_id)
            last_count = len(rows)
            match = next(
                (
                    row
                    for row in reversed(rows)
                    if row.get("event") == "afterAgentResponse"
                    and nonce in str(row.get("text") or "")
                ),
                None,
            )
            if match is not None:
                self.observe(
                    case_id=case_id,
                    provider=self.args.provider,
                    ownership=ownership,
                    source="cursor_native_hooks",
                    event="assistant_response_local",
                    session_id=session_id,
                    payload={"hook_root": str(hook_root), "hook_event": match},
                )
                return match
            time.sleep(interval)
        self.observe(
            case_id=case_id,
            provider=self.args.provider,
            ownership=ownership,
            source="cursor_native_hooks",
            event="assistant_response_local_timeout",
            session_id=session_id,
            payload={"hook_root": str(hook_root), "last_event_count": last_count},
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
        return {
            "case_id": case_id,
            "session_id": session_id,
            "nonce": nonce,
            "rollout": str(rollout),
        }

    def write_snapshot(
        self, case_id: str, ownership: str, session_id: str, label: str
    ) -> None:
        local = call_or_error(lambda: self.local_health(session_id))
        hosted_debug = call_or_error(lambda: self.hosted_debug(session_id))
        hosted_direct = call_or_error(lambda: self.hosted_db_direct(session_id))
        hosted = {
            **(hosted_direct or {}),
            **(hosted_debug or {}),
        }
        if hosted_direct and hosted_direct.get("live_session_catalog") is not None:
            hosted["live_session_catalog"] = hosted_direct["live_session_catalog"]
        timeline = call_or_error(lambda: self.timeline_session(session_id))
        sse = call_or_error(lambda: self.timeline_sse_initial_replay(session_id))
        payload = {
            "local_health": local,
            "hosted_debug": compact_hosted(hosted),
            "timeline": compact_timeline(timeline or {}),
            "empty_shell_projection": empty_shell_projection_proof(
                hosted, timeline or {}
            ),
            "content_promotion_projection": content_promotion_projection_proof(
                hosted, timeline or {}
            ),
            "sse": sse,
        }
        path = self.output_dir / f"{case_id}-{label}-{session_id}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
        self.observe(
            case_id=case_id,
            provider=self.args.provider,
            ownership=ownership,
            source="harness",
            event=f"snapshot_{label}",
            session_id=session_id,
            payload={"path": str(path), **compact_snapshot(payload)},
        )

    def write_summary(
        self, results: list[dict[str, Any]], errors: list[str]
    ) -> list[dict[str, Any]]:
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
                    "sla_status": self.sla_case.get("status")
                    if self.sla_case
                    else None,
                    "project": self.project,
                    "subdomain": self.subdomain,
                    "generated_at": utc_now(),
                    "targets": {
                        "live_first_output_ms": live_first_output_target_ms(),
                        "durable_archive_ms": durable_archive_target_ms(),
                        "content_promotion_ms": content_promotion_target_ms(),
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

    def verdict_for(
        self, case_id: str, session_id: str, nonce: str
    ) -> tuple[str, str, dict[str, Any]]:
        active_metrics = (
            set(self.sla_case.get("metrics") or []) if self.sla_case else set()
        )
        requires_promotion = (
            not active_metrics
            or "content_durable_to_timeline_card_paint_ms" in active_metrics
        )
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
        requires_durable = (
            not active_metrics or "durable_archive_local_to_hosted_ms" in active_metrics
        )
        hosted = self.hosted_db_direct(session_id) or {}
        if not hosted.get("session"):
            # The catalog-owned live store and the debug helper can briefly
            # observe different SQLite generations during archive promotion.
            # Keep the direct reader as the primary timing source, but use the
            # same canonical debug snapshot for final verdict identity when it
            # has the durable row the direct read missed.
            hosted_fallback = self.hosted_debug(session_id) or {}
            if hosted_fallback.get("session"):
                hosted = {**hosted, **hosted_fallback}
        session = hosted.get("session") or {}
        runtime = hosted.get("runtime_state") or {}
        waterfall_report = self.hosted_latency_report(session_id) or {}
        browser_render_beacons = self.browser_client_render_beacons(case_id, session_id)
        web_waterfall = select_propagation_waterfall(waterfall_report, surface="web")
        if web_waterfall is None:
            web_waterfall = select_live_beacon_waterfall(
                browser_render_beacons,
                surface="web",
            )
        ios_waterfall = select_propagation_waterfall(waterfall_report, surface="ios")
        state_settlement = self.runtime_state_settlement_metrics(case_id, session_id)
        client_state = state_render_beacon_metrics(
            hosted,
            browser_render_beacons,
        )
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
        content_promotion_raw_latency = self.event_delta_any_order_ms(
            case_id,
            session_id,
            "content_durable_published",
            "browser_timeline_card_painted",
        )
        content_promotion_latency = valid_monotonic_delta_ms(
            content_promotion_raw_latency
        )
        content_promotion_order_valid = (
            content_promotion_raw_latency is not None
            and content_promotion_raw_latency >= 0
            if content_promotion_raw_latency is not None
            else None
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
            "browser_live_transcript_first_painted",
        )
        if browser_live_first_from_local_latency is None:
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
            "browser_live_transcript_nonce_painted",
        )
        if browser_live_full_from_local_latency is None:
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
            if self.event_observed_at_ms(
                case_id, session_id, "browser_workspace_preview_stream_changed"
            )
            is not None
            else "browser_workspace_stream_changed"
        )
        browser_workspace_to_first_paint_latency = (
            self.event_payload_elapsed_delta_nearest_before_ms(
                case_id,
                session_id,
                workspace_stream_event,
                "browser_live_transcript_first_painted",
            )
        )
        if browser_workspace_to_first_paint_latency is None:
            browser_workspace_to_first_paint_latency = self.event_delta_any_order_ms(
                case_id,
                session_id,
                workspace_stream_event,
                "browser_live_transcript_first_painted",
            )
        browser_workspace_to_tail_paint_latency = (
            self.event_payload_elapsed_delta_nearest_before_ms(
                case_id,
                session_id,
                workspace_stream_event,
                "browser_live_transcript_nonce_painted",
            )
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
        transport_failure = self.transport_failure_classification(
            case_id, session_id, latest_health_state
        )
        ownership = qualification_ownership(session, self.args.ownership)
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
            "content_durable_to_timeline_card_paint_ms": content_promotion_latency,
            "content_durable_to_timeline_card_paint_raw_ms": content_promotion_raw_latency,
            "content_durable_to_timeline_card_paint_target_ms": content_promotion_target_ms(),
            "content_durable_to_timeline_card_paint_pass": (
                content_promotion_latency <= content_promotion_target_ms()
                if content_promotion_latency is not None
                and content_promotion_order_valid is True
                else None
            ),
            "content_promotion_order_valid": content_promotion_order_valid,
            "content_promotion_baseline_observed": self.event_observed_at_ms(
                case_id,
                session_id,
                "empty_shell_observed",
            )
            is not None,
            "content_promotion_published_observed": self.event_observed_at_ms(
                case_id,
                session_id,
                "content_durable_published",
            )
            is not None,
            "content_promotion_observation_interval_ms": self.event_payload_int(
                case_id,
                session_id,
                "content_durable_published",
                "observation_interval_ms",
            ),
            "content_promotion_clock": "profiler_monotonic",
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
            "browser_runtime_state_stream_to_paint_ms": state_settlement.get(
                "stream_to_paint_ms"
            ),
            "browser_runtime_state_fanout_to_paint_ms": state_settlement.get(
                "fanout_to_paint_ms"
            ),
            "browser_runtime_state_settlement_count": state_settlement.get("count", 0),
            "web_state_render_beacon_count": client_state.get("web_count", 0),
            "web_state_render_beacon_p50_ms": client_state.get("web_p50_ms"),
            "web_state_render_beacon_p95_ms": client_state.get("web_p95_ms"),
            "web_state_render_beacon_source": client_state.get("source"),
            "ios_state_render_beacon_count": client_state.get("ios_count", 0),
            "ios_state_render_beacon_p50_ms": client_state.get("ios_p50_ms"),
            "ios_state_render_beacon_p95_ms": client_state.get("ios_p95_ms"),
            "state_render_beacon_count": client_state.get("count", 0),
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
            "propagation_waterfall": {
                "web": web_waterfall,
                "ios": ios_waterfall,
                "report_gaps": waterfall_report.get("gaps") or [],
                "known_unimplemented_probes": waterfall_report.get(
                    "known_unimplemented_probes"
                )
                or [],
            },
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
        if web_waterfall:
            metrics["waterfall_total_provider_to_first_render_ms"] = web_waterfall.get(
                "total_provider_to_first_render_ms"
            )
            for stage_key in WATERFALL_STAGE_KEYS:
                metrics[f"waterfall_{stage_key}_ms"] = (
                    (web_waterfall.get("stages") or {}).get(stage_key) or {}
                ).get("duration_ms")
        else:
            metrics["waterfall_total_provider_to_first_render_ms"] = None
            for stage_key in WATERFALL_STAGE_KEYS:
                metrics[f"waterfall_{stage_key}_ms"] = None
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
        metrics["live_first_source"] = (
            live_ui_source if live_first_from_local_latency is not None else None
        )
        metrics["live_first_timing_source"] = (
            live_timing_source if live_first_from_local_latency is not None else None
        )
        metrics["live_tail_non_slo_from_local_ms"] = live_full_from_local_latency
        if live_first_from_local_latency is not None:
            metrics["live_first_pass"] = (
                live_first_from_local_latency <= live_first_output_target_ms()
            )

        promotion_note = "promotion=not_applicable"
        if requires_promotion:
            if content_promotion_raw_latency is None:
                promotion_note = "promotion=missing"
            elif content_promotion_order_valid is not True:
                promotion_note = (
                    "promotion=inconclusive "
                    f"content_to_card_raw={content_promotion_raw_latency}ms"
                )
            else:
                promotion_state = (
                    "pass"
                    if metrics["content_durable_to_timeline_card_paint_pass"] is True
                    else "slow"
                )
                promotion_note = (
                    f"promotion={promotion_state} "
                    f"content_to_card={content_promotion_latency}ms "
                    f"target={content_promotion_target_ms()}ms "
                    f"poll={metrics.get('content_promotion_observation_interval_ms', '-')}ms"
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
                live_ui += (
                    f" live_tail_non_slo_from_local={live_full_from_local_latency}ms"
                )

        cold_note = "cold=not_run"
        if requires_cold:
            cold_parts = []
            if cold_card_latency is not None:
                card_state = (
                    "pass"
                    if cold_card_latency <= cold_timeline_card_target_ms()
                    else "slow"
                )
                cold_parts.append(
                    f"card={card_state} nav_to_card={cold_card_latency}ms target={cold_timeline_card_target_ms()}ms"
                )
            else:
                cold_parts.append("card=missing")
            if cold_close_latency is not None:
                close_state = (
                    "pass"
                    if cold_close_latency <= cold_timeline_close_target_ms()
                    else "slow"
                )
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
            if (
                propagation_latency is not None
                and propagation_latency != durable_archive_latency
            ):
                transcript += f" poll_observed={propagation_latency}ms"
        if card_latency is not None:
            transcript += f" timeline_card_pre_ingest={card_latency}ms"
        if browser_card_latency is not None:
            transcript += f" browser_card_from_session_id={browser_card_latency}ms"
        if content_promotion_raw_latency is not None:
            transcript += (
                f" content_to_browser_card_raw={content_promotion_raw_latency}ms"
            )
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
            transcript += (
                f" first_live_http_from_local={first_live_http_from_local_latency}ms"
            )
        if live_http_from_local_latency is not None:
            transcript += f" live_http_from_local={live_http_from_local_latency}ms"
        if first_live_sse_from_local_latency is not None:
            transcript += (
                f" first_live_sse_from_local={first_live_sse_from_local_latency}ms"
            )
        if live_sse_from_local_latency is not None:
            transcript += f" live_sse_from_local={live_sse_from_local_latency}ms"
        if browser_live_first_from_local_latency is not None:
            transcript += f" browser_first_live_from_local={browser_live_first_from_local_latency}ms"
        if browser_live_full_from_local_latency is not None:
            transcript += (
                f" browser_live_from_local={browser_live_full_from_local_latency}ms"
            )
        if browser_live_first_from_local_wall_latency is not None:
            transcript += f" browser_first_live_from_local_wall={browser_live_first_from_local_wall_latency}ms"
        if browser_live_full_from_local_wall_latency is not None:
            transcript += f" browser_live_from_local_wall={browser_live_full_from_local_wall_latency}ms"
        if browser_live_first_from_live_truth_wall_latency is not None:
            transcript += f" browser_first_live_from_live_truth={browser_live_first_from_live_truth_wall_latency}ms"
        if browser_live_full_from_live_truth_wall_latency is not None:
            transcript += f" browser_live_from_live_truth={browser_live_full_from_live_truth_wall_latency}ms"
        if browser_first_after_sse_latency is not None:
            transcript += (
                f" sse_to_browser_first_live={browser_first_after_sse_latency}ms"
            )
        if browser_full_after_sse_latency is not None:
            transcript += f" sse_to_browser_live={browser_full_after_sse_latency}ms"
        if browser_workspace_to_first_paint_latency is not None:
            transcript += f" browser_workspace_stream_to_first_paint={browser_workspace_to_first_paint_latency}ms"
        if browser_workspace_to_tail_paint_latency is not None:
            transcript += f" browser_workspace_stream_to_tail_paint={browser_workspace_to_tail_paint_latency}ms"
        if browser_workspace_after_sse_latency is not None:
            transcript += f" browser_workspace_stream_after_sse={browser_workspace_after_sse_latency}ms"
        if state_settlement.get("count"):
            transcript += (
                f" runtime_state_settlements={state_settlement['count']}"
                f" stream_to_paint_p50={state_settlement.get('stream_to_paint_ms')}ms"
                f" fanout_to_paint_p50={state_settlement.get('fanout_to_paint_ms')}ms"
            )
        if client_state.get("count"):
            transcript += (
                f" state_beacons={client_state['count']}"
                f" web_p50={client_state.get('web_p50_ms')}ms"
                f" ios_p50={client_state.get('ios_p50_ms')}ms"
            )
        if transcript_ingest.get("ingest_lag_ms") is not None:
            transcript += f" server_ingest_lag={transcript_ingest['ingest_lag_ms']}ms"
        if transcript_ingest.get("skew_adjusted_lag_ms") is not None:
            transcript += (
                f" skew_adjusted_ingest={transcript_ingest['skew_adjusted_lag_ms']}ms"
            )
        if durable_archive_latency is not None:
            durable_state = (
                "pass"
                if durable_archive_latency <= durable_archive_target_ms()
                else "slow"
            )
            metrics["durable_archive_pass"] = (
                durable_archive_latency <= durable_archive_target_ms()
            )
            transcript += f" durable_archive={durable_state} target={durable_archive_target_ms()}ms"
        bridge_live = bridge_live_details(hosted, nonce, self.remote_clock_skew_ms)
        if bridge_live:
            metrics["bridge_live_ingest_lag_ms"] = bridge_live.get("ingest_lag_ms")
            metrics["bridge_live_skew_adjusted_lag_ms"] = bridge_live.get(
                "skew_adjusted_lag_ms"
            )
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
                close_state = (
                    "pass" if close_latency <= managed_close_target_ms() else "slow"
                )
                metrics["close_pass"] = close_latency <= managed_close_target_ms()
                close_note += (
                    f" close_slo={close_state} target={managed_close_target_ms()}ms"
                )
                if close_sse_latency is not None and close_http_latency is not None:
                    close_note += f" http_observed_in={close_http_latency}ms"
                if close_browser_latency is not None:
                    close_note += f" browser_observed_in={close_browser_latency}ms"
                if close_browser_after_http_latency is not None:
                    close_note += (
                        f" http_to_browser={close_browser_after_http_latency}ms"
                    )
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
            metrics["notes"] = (
                f"{note}; {close_note}; ownership={ownership}, transport={transport}"
            )
            return "blocked", metrics["notes"], metrics
        if metrics["provider_timeout"]:
            metrics["verdict"] = "provider_timeout"
            metrics["notes"] = (
                f"provider_timeout=true; {live_ui}; transcript={transcript}; "
                f"{close_note}; ownership={ownership}, transport={transport}"
            )
            return "provider_timeout", metrics["notes"], metrics
        if transport_failure is not None and (
            (
                requires_promotion
                and metrics["content_durable_to_timeline_card_paint_pass"] is not True
            )
            or (requires_live and metrics["live_first_pass"] is not True)
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
            metrics["notes"] = (
                f"{live_ui}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            )
            return verdict, metrics["notes"], metrics
        is_managed_case = case_id == "B1" or ownership in {"managed", "managed_local"}
        if (
            requires_promotion
            and is_managed_case
            and metrics["content_durable_to_timeline_card_paint_pass"] is not True
        ):
            verdict = "missing" if content_promotion_latency is None else "slow"
            metrics["verdict"] = verdict
            metrics["notes"] = (
                f"{promotion_note}; {live_ui}; transcript={transcript}; {close_note}; "
                f"ownership={ownership}, transport={transport}"
            )
            return verdict, metrics["notes"], metrics
        if requires_live and is_managed_case and metrics["live_first_pass"] is not True:
            metrics["verdict"] = "fail"
            metrics["notes"] = (
                f"{promotion_note}; {live_ui}; transcript={transcript}; {close_note}; "
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
            metrics["notes"] = (
                f"{live_ui}; {cold_note}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            )
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
            metrics["notes"] = (
                f"{live_ui}; nonce synced; close not confirmed yet; phase={phase}; ownership={ownership}, transport={transport}"
            )
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
            metrics["notes"] = (
                f"{live_ui}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            )
            return "slow", metrics["notes"], metrics
        if requires_durable and metrics["durable_archive_pass"] is False:
            metrics["verdict"] = "slow"
            metrics["notes"] = (
                f"{live_ui}; transcript={transcript}; {close_note}; ownership={ownership}, transport={transport}"
            )
            return "slow", metrics["notes"], metrics
        metrics["verdict"] = "pass"
        extra = f"; {cold_note}" if requires_cold else ""
        promotion_extra = f"{promotion_note}; " if requires_promotion else ""
        metrics["notes"] = (
            f"{promotion_extra}{live_ui}{extra}; transcript={transcript}; {close_note}; "
            f"ownership={ownership}, transport={transport}"
        )
        return "pass", metrics["notes"], metrics

    def browser_client_render_beacons(
        self, case_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        """Return browser beacon payloads when live-catalog persistence is unavailable."""
        beacons: list[dict[str, Any]] = []
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") not in {
                "browser_client_render_beacon_request",
                "browser_cold_client_render_beacon_request",
                "browser_client_render_beacon_payload",
                "browser_cold_client_render_beacon_payload",
            }:
                continue
            payload = row.get("payload")
            values = payload.get("beacons") if isinstance(payload, dict) else None
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                if str(value.get("session_id") or "") != session_id:
                    continue
                beacons.append(value)
        return beacons

    def event_delta_ms(
        self, case_id: str, session_id: str, start_event: str, end_event: str
    ) -> int | None:
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

    def event_observed_at_ms(
        self, case_id: str, session_id: str, event: str
    ) -> int | None:
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

    def event_payload_wall_ms(
        self, case_id: str, session_id: str, event: str
    ) -> int | None:
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

    def event_payload_elapsed_ms(
        self, case_id: str, session_id: str, event: str
    ) -> int | None:
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

    def event_payload_int(
        self, case_id: str, session_id: str, event: str, key: str
    ) -> int | None:
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") != event:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            value = int_or_none(payload.get(key))
            if value is not None:
                return value
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

    def runtime_state_settlement_metrics(
        self, case_id: str, session_id: str
    ) -> dict[str, Any]:
        """Match each browser state paint to the catalog commit that woke it."""
        streams: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") not in {
                "browser_workspace_stream_changed",
                "browser_cold_workspace_stream_changed",
            }:
                continue
            payload = row.get("payload")
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(detail, dict):
                continue
            commit_seq = int_or_none(detail.get("catalog_commit_seq"))
            observed = row.get("observed_at_monotonic_ms")
            if commit_seq is None or not isinstance(observed, int):
                continue
            streams.setdefault(commit_seq, []).append((observed, detail))

        settlements: list[dict[str, int]] = []
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") not in {
                "browser_runtime_state_painted",
                "browser_cold_runtime_state_painted",
            }:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            commit_seq = int_or_none(payload.get("stream_catalog_commit_seq"))
            observed = row.get("observed_at_monotonic_ms")
            candidates = streams.get(commit_seq or 0, [])
            if commit_seq is None or not isinstance(observed, int) or not candidates:
                continue
            previous = [item for item in candidates if item[0] <= observed]
            if not previous:
                continue
            stream_observed, detail = max(previous, key=lambda item: item[0])
            item = {"stream_to_paint_ms": observed - stream_observed}
            fanout_at_ms = int_or_none(detail.get("server_fanout_at_ms"))
            paint_at_ms = payload_wall_timestamp(row)
            if (
                fanout_at_ms is not None
                and paint_at_ms is not None
                and paint_at_ms >= fanout_at_ms
            ):
                item["fanout_to_paint_ms"] = paint_at_ms - fanout_at_ms
            settlements.append(item)

        stream_values = [item["stream_to_paint_ms"] for item in settlements]
        fanout_values = [
            item["fanout_to_paint_ms"]
            for item in settlements
            if "fanout_to_paint_ms" in item
        ]
        return {
            "count": len(settlements),
            "stream_to_paint_ms": percentile(stream_values, 50),
            "fanout_to_paint_ms": percentile(fanout_values, 50),
            "settlements": settlements,
        }

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

    def provider_precondition_for(
        self, case_id: str, session_id: str
    ) -> dict[str, Any] | None:
        for row in self.observations:
            if row.get("case_id") != case_id or row.get("session_id") != session_id:
                continue
            if row.get("event") == "provider_precondition_blocked":
                payload = row.get("payload")
                if isinstance(payload, dict):
                    return payload
                return {}
        return None

    def latest_local_health_summary(
        self, case_id: str, session_id: str
    ) -> dict[str, Any]:
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


def select_propagation_waterfall(
    report: dict[str, Any], *, surface: str
) -> dict[str, Any] | None:
    """Select the newest rendered assistant event for one client surface."""

    candidates: list[dict[str, Any]] = []
    for event in report.get("events") or []:
        if not isinstance(event, dict) or event.get("role") != "assistant":
            continue
        renders = [
            render
            for render in event.get("client_renders") or []
            if isinstance(render, dict) and render.get("surface") == surface
        ]
        if not renders:
            continue
        candidates.append(
            {**event, "client_renders": renders, "first_client_render": renders[0]}
        )
    if not candidates:
        return None
    selected = max(
        candidates, key=lambda event: int_or_none(event.get("event_id")) or 0
    )
    return {
        "event_id": selected.get("event_id"),
        "surface": surface,
        "total_provider_to_first_render_ms": selected.get(
            "total_provider_to_first_render_ms"
        ),
        "measured_total_ms": selected.get("measured_total_ms"),
        "unaccounted_ms": selected.get("unaccounted_ms"),
        "client_clock_skew_ms": selected.get("client_clock_skew_ms"),
        "bottleneck": selected.get("bottleneck"),
        "gaps": selected.get("gaps") or [],
        "first_client_render": selected.get("first_client_render"),
        "stages": {
            stage.get("key"): stage
            for stage in selected.get("stages") or []
            if isinstance(stage, dict) and stage.get("key") in WATERFALL_STAGE_KEYS
        },
    }


def select_live_beacon_waterfall(
    beacons: list[dict[str, Any]], *, surface: str
) -> dict[str, Any] | None:
    """Build the live-overlay waterfall when archive observation tables are absent."""

    candidates = [
        beacon
        for beacon in beacons
        if beacon.get("surface") == surface
        and beacon.get("ship_trace_id")
        and isinstance(beacon.get("provider_observed_at_ms"), int)
    ]
    if not candidates:
        return None
    beacon = max(
        candidates,
        key=lambda value: int_or_none(value.get("rendered_at_ms")) or 0,
    )
    skew = int_or_none(beacon.get("clock_skew_ms")) or 0
    provider_observed = int_or_none(beacon.get("provider_observed_at_ms"))
    engine_enqueued = int_or_none(beacon.get("engine_enqueued_at_ms"))
    job_started = int_or_none(beacon.get("engine_job_started_at_ms"))
    http_send = int_or_none(beacon.get("engine_http_send_started_at_ms"))
    server_handler = int_or_none(beacon.get("server_handler_entered_at_ms"))
    server_fanout = int_or_none(beacon.get("server_fanout_at_ms"))
    client_received_raw = int_or_none(beacon.get("client_received_at_ms"))
    rendered_raw = int_or_none(beacon.get("rendered_at_ms"))
    client_received = (
        client_received_raw - skew if client_received_raw is not None else None
    )
    rendered = rendered_raw - skew if rendered_raw is not None else None

    coordinates = {
        "provider_to_engine_observed": (provider_observed, provider_observed),
        "engine_observed_to_enqueued": (provider_observed, engine_enqueued),
        "engine_enqueued_to_job_started": (engine_enqueued, job_started),
        "engine_job_started_to_http_send": (job_started, http_send),
        "http_send_to_server_handler": (http_send, server_handler),
        "server_handler_to_store_returned": (None, None),
        "server_store_to_fanout": (None, None),
        "server_fanout_to_client_received": (server_fanout, client_received),
        "client_received_to_rendered": (client_received, rendered),
    }
    stages = {}
    for key, (started_at, ended_at) in coordinates.items():
        duration = (
            max(0, ended_at - started_at)
            if started_at is not None and ended_at is not None
            else None
        )
        stages[key] = {
            "key": key,
            "duration_ms": duration,
            "confidence": "observed"
            if key in {"engine_observed_to_enqueued", "engine_enqueued_to_job_started", "engine_job_started_to_http_send", "client_received_to_rendered"}
            else "derived",
            "source": "live_render_beacon",
        }
    measured = [
        stage for stage in stages.values() if isinstance(stage.get("duration_ms"), int)
    ]
    bottleneck = max(measured, key=lambda stage: stage["duration_ms"]) if measured else None
    total = (
        max(0, rendered - provider_observed)
        if rendered is not None and provider_observed is not None
        else None
    )
    return {
        "event_id": beacon.get("event_id"),
        "surface": surface,
        "trace_id": beacon.get("ship_trace_id"),
        "total_provider_to_first_render_ms": total,
        "measured_total_ms": sum(stage["duration_ms"] for stage in measured),
        "unaccounted_ms": None,
        "client_clock_skew_ms": skew,
        "bottleneck": bottleneck,
        "gaps": ["durable_store_is_not_on_live_preview_critical_path"],
        "first_client_render": beacon,
        "stages": stages,
    }


def qualification_ownership(session: dict[str, Any], requested: str | None) -> str:
    return str(session.get("execution_home") or requested or "-")


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
    match = re.search(
        r"(?:Session ID:|Attach:\s+longhouse\s+codex\s+attach\s+--session-id)\s*([0-9a-fA-F-]{36})",
        text,
    )
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
    # Catalog-backed hosted tenants keep the durable transcript in `sessions`
    # while live provisional text is held separately.  The profiler must be
    # able to prove the archived assistant result in either schema.
    for key in (
        "last_assistant_message_preview",
        "last_visible_text_preview",
        "transcript_preview",
    ):
        if text in str((data.get("session") or {}).get(key) or ""):
            return True
        if text in str((data.get("archive_session") or {}).get(key) or ""):
            return True
    # Storage-v2 keeps the durable assistant count and the probe's first user
    # prompt on the session row, but provider-specific preview updates may
    # replace last_visible_text_preview with a shell/system rendering. The
    # exact response is already proven by the local provider hook and browser
    # workspace; bind that proof to the same hosted session by requiring the
    # nonce-bearing prompt plus a durable assistant message.
    for key in ("storage_session", "archive_session", "session"):
        row = data.get(key)
        if not isinstance(row, dict):
            continue
        if text not in str(row.get("first_user_message_preview") or ""):
            continue
        try:
            assistant_messages = int(row.get("assistant_messages") or 0)
        except (TypeError, ValueError):
            assistant_messages = 0
        if assistant_messages > 0:
            return True
    return False


def hosted_catalog_row(data: dict[str, Any]) -> dict[str, Any]:
    row = data.get("live_session_catalog")
    return row if isinstance(row, dict) else {}


def hosted_served_row(data: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Return the row that backs the default served timeline when available.

    Current hosted tenants can contain both the legacy live catalog and the
    durable ``sessions`` storage projection.  ``session.timeline.list.v2``
    unions those paths and suppresses duplicate legacy rows when a storage row
    exists, so a promotion probe must follow the same authority order.
    """

    storage = data.get("storage_session")
    if isinstance(storage, dict) and storage:
        return "sessions", storage
    legacy = hosted_catalog_row(data)
    if legacy:
        return "live_session_catalog", legacy
    return None, {}


def hosted_content_counts(data: dict[str, Any]) -> dict[str, int]:
    _source, row = hosted_served_row(data)
    return {
        field: int_or_none(row.get(field)) or 0
        for field in ("user_messages", "assistant_messages", "tool_calls")
    }


def hosted_empty_shell(data: dict[str, Any]) -> bool:
    _source, row = hosted_served_row(data)
    hidden = int_or_none(row.get("hidden_from_default_timeline"))
    counts = hosted_content_counts(data)
    return hidden == 1 and all(value == 0 for value in counts.values())


def hosted_content_published(data: dict[str, Any]) -> bool:
    _source, row = hosted_served_row(data)
    hidden = int_or_none(row.get("hidden_from_default_timeline"))
    counts = hosted_content_counts(data)
    return hidden == 0 and any(value > 0 for value in counts.values())


def valid_monotonic_delta_ms(value: int | None) -> int | None:
    return value if value is not None and value >= 0 else None


def empty_shell_projection_proof(
    hosted: dict[str, Any],
    timeline: dict[str, Any],
) -> dict[str, Any]:
    source, row = hosted_served_row(hosted)
    matches = timeline.get("matches") or []
    listing_status = timeline.get("listing_status")
    return {
        "catalog_source": source,
        "hidden_from_default_timeline": int_or_none(
            row.get("hidden_from_default_timeline")
        ),
        "content_counts": hosted_content_counts(hosted),
        "default_listing_status": listing_status,
        "default_listing_contains_session": bool(matches),
        "proven": hosted_empty_shell(hosted) and listing_status == 200 and not matches,
    }


def content_promotion_projection_proof(
    hosted: dict[str, Any],
    timeline: dict[str, Any],
) -> dict[str, Any]:
    source, row = hosted_served_row(hosted)
    matches = timeline.get("matches") or []
    listing_status = timeline.get("listing_status")
    return {
        "catalog_source": source,
        "hidden_from_default_timeline": int_or_none(
            row.get("hidden_from_default_timeline")
        ),
        "content_counts": hosted_content_counts(hosted),
        "default_listing_status": listing_status,
        "default_listing_contains_session": bool(matches),
        "proven": hosted_content_published(hosted)
        and listing_status == 200
        and bool(matches),
    }


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
        payload_source = (
            str(payload.get("terminal_source") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
        event_source = str(event.get("source") or "").strip()
        candidate = _terminal_details_from_event(
            event, payload if isinstance(payload, dict) else {}
        )
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


def _terminal_details_from_event(
    event: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    occurred_at = parse_db_timestamp(event.get("occurred_at"))
    received_at = parse_db_timestamp(event.get("received_at"))
    details: dict[str, Any] = {
        "source": str(event.get("source") or "").strip() or None,
        "state": str(payload.get("terminal_state") or "").strip() or None,
        "reason": str(payload.get("terminal_reason") or "").strip() or None,
        "ingest_lag_ms": None,
        "observed_at_ms": int(occurred_at.timestamp() * 1000)
        if occurred_at is not None
        else None,
        "received_at_ms": int(received_at.timestamp() * 1000)
        if received_at is not None
        else None,
    }
    payload_source = str(payload.get("terminal_source") or "").strip()
    if payload_source:
        details["source"] = payload_source
    if occurred_at is not None and received_at is not None:
        details["ingest_lag_ms"] = int(
            (received_at - occurred_at).total_seconds() * 1000
        )
    return details


def _merge_terminal_details(details: dict[str, Any], candidate: dict[str, Any]) -> None:
    for key in (
        "state",
        "reason",
        "source",
        "ingest_lag_ms",
        "observed_at_ms",
        "received_at_ms",
    ):
        if details.get(key) is None and candidate.get(key) is not None:
            details[key] = candidate[key]


def transcript_ingest_details(
    data: dict[str, Any], remote_clock_skew_ms: int | None
) -> dict[str, Any]:
    details = {
        "ingest_lag_ms": None,
        "skew_adjusted_lag_ms": None,
    }
    for event in data.get("runtime_observations") or []:
        if event.get("kind") != "progress_signal":
            continue
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        if (
            not isinstance(payload, dict)
            or payload.get("progress_kind") != "transcript_append"
        ):
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
        if (
            not isinstance(payload, dict)
            or payload.get("progress_kind") != "bridge_live_transcript_delta"
        ):
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


def ship_trace_details(
    data: dict[str, Any], remote_clock_skew_ms: int | None
) -> dict[str, Any]:
    for event in data.get("runtime_observations") or []:
        if event.get("source") != "agents_ingest_trace":
            continue
        payload = safe_json_loads(str(event.get("payload_json") or "")) or {}
        if (
            not isinstance(payload, dict)
            or payload.get("progress_kind") != "ship_pipeline_trace"
        ):
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
        if (
            isinstance(payload, dict)
            and payload.get("progress_kind") == "transcript_append"
        ):
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
        "archive_session": data.get("archive_session"),
        "storage_session": data.get("storage_session"),
        "live_session_catalog": data.get("live_session_catalog"),
        "timeline_card": data.get("timeline_card"),
        "runtime_state": data.get("runtime_state"),
        "event_stats": data.get("event_stats"),
        "recent_events": (data.get("recent_events") or [])[:5],
        "runtime_observations": (data.get("runtime_observations") or [])[:5],
        "client_render_observation_stats": data.get("client_render_observation_stats"),
        "client_render_observations": (
            data.get("client_render_observations")
            or data.get("recent_client_render_observations")
            or []
        )[:20],
    }


def state_render_beacon_metrics(
    data: dict[str, Any],
    browser_beacons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = (
        data.get("client_render_observations")
        or data.get("recent_client_render_observations")
        or []
    )
    by_surface: dict[str, list[int]] = {"web": [], "ios": []}
    source = "persisted"
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if payload.get("render_kind") != "state":
            continue
        surface = str(payload.get("surface") or "").strip().lower()
        latency = int_or_none(payload.get("latency_ms"))
        if surface in by_surface and latency is not None and latency >= 0:
            by_surface[surface].append(latency)
    # Live-catalog tenants intentionally do not persist client observations in
    # the legacy SQL store. The browser observer captures the exact beacon
    # request instead, preserving a real client-side settlement measurement.
    if not any(by_surface.values()):
        source = "browser_request"
        for payload in browser_beacons or []:
            if str(payload.get("render_kind") or "") != "state":
                continue
            surface = str(payload.get("surface") or "").strip().lower()
            emitted = int_or_none(payload.get("emitted_at_ms"))
            rendered = int_or_none(payload.get("rendered_at_ms"))
            skew = int_or_none(payload.get("clock_skew_ms")) or 0
            if surface not in by_surface or emitted is None or rendered is None:
                continue
            latency = rendered - skew - emitted
            if latency >= 0:
                by_surface[surface].append(latency)
    all_values = by_surface["web"] + by_surface["ios"]
    return {
        "source": source if any(by_surface.values()) else None,
        "count": len(all_values),
        "web_count": len(by_surface["web"]),
        "web_p50_ms": percentile(by_surface["web"], 50),
        "web_p95_ms": percentile(by_surface["web"], 95),
        "ios_count": len(by_surface["ios"]),
        "ios_p50_ms": percentile(by_surface["ios"], 50),
        "ios_p95_ms": percentile(by_surface["ios"], 95),
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
                    "transcript_preview": (card.get("head") or {}).get(
                        "transcript_preview"
                    ),
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
            if isinstance(value, dict) and isinstance(
                value.get("transcript_preview"), dict
            ):
                transcripts.append(value["transcript_preview"])
    return transcripts


def compact_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "local_health": payload.get("local_health"),
        "hosted_debug": payload.get("hosted_debug"),
        "timeline": payload.get("timeline"),
        "empty_shell_projection": payload.get("empty_shell_projection"),
        "content_promotion_projection": payload.get("content_promotion_projection"),
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


def terminate_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
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
    return str(hook.get("command") or "") == str(CODEX_LONGHOUSE_HOOK_SCRIPT) and str(
        hook.get("sourcePath") or ""
    ) == str(CODEX_HOOKS_JSON)


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

    before = [
        compact(hook) for hook in result.get("before") or [] if isinstance(hook, dict)
    ]
    after = [
        compact(hook) for hook in result.get("after") or [] if isinstance(hook, dict)
    ]
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
        return {
            "error": f"timeout after {exc.timeout}s",
            "cmd": redact_cmd(list(exc.cmd))
            if isinstance(exc.cmd, list)
            else str(exc.cmd),
        }
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
    parser.add_argument(
        "--provider", choices=["claude", "codex", "cursor", "opencode"], default="codex"
    )
    parser.add_argument(
        "--ownership", choices=["managed", "unmanaged", "all"], default="all"
    )
    parser.add_argument(
        "--subdomain", default=os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN", "demo")
    )
    parser.add_argument("--container")
    parser.add_argument(
        "--ssh-target",
        default=os.environ.get("HOSTED_SESSION_DEBUG_SSH_TARGET", "zerg"),
        help="SSH host that runs the hosted Runtime Host and tenant containers.",
    )
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
        choices=[
            "cold_timeline",
            "warm_realtime",
            "durable_archive",
            "honest_degradation",
            "fidelity",
        ],
        default=None,
        help="Observation profile class metadata. Narrow --profile modes will map to this in later slices.",
    )
    parser.add_argument(
        "--browser-ui-base-url",
        help="Hosted browser UI origin to profile. Defaults to https://<subdomain>.longhouse.ai.",
    )
    parser.add_argument(
        "--browser-transport",
        choices=["default", "disable-quic"],
        default="default",
        help="Browser transport lane. disable-quic isolates app settlement from Chromium HTTP/3 transport failures.",
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
    parser.add_argument(
        "--cursor-model",
        default="gpt-5.3-codex-low",
        help="Model passed to the native Cursor Helm canary without changing the user's normal Cursor configuration.",
    )
    parser.add_argument(
        "--cursor-workspace",
        help="Isolated workspace for the native Cursor Helm canary. Defaults under ~/.longhouse/canaries/provider-live.",
    )
    parser.add_argument(
        "--opencode-workspace",
        help="Isolated workspace for the native OpenCode Helm canary. Defaults under ~/.longhouse/canaries/provider-live.",
    )
    return parser.parse_args(argv)


def normalize_args(args: argparse.Namespace) -> None:
    if args.profile in {"cold-timeline", "warm-live"}:
        if args.skip_browser_ui:
            raise SystemExit(
                f"--profile {args.profile} requires the browser UI observer"
            )
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
            "browser_transport": args.browser_transport,
            "profile_class": profiler.profile_class,
            "sla_case_id": profiler.sla_case.get("id") if profiler.sla_case else None,
            "sla_status": profiler.sla_case.get("status")
            if profiler.sla_case
            else None,
            "sla_manifest": str(DEFAULT_MANIFEST_PATH),
            "sla_manifest_summary": manifest_summary(sla_manifest()),
        },
    )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        if (
            args.ownership in {"managed", "all"}
            and not args.skip_managed
            and args.provider == "codex"
        ):
            results.append(profiler.run_managed_codex())
        elif (
            args.ownership in {"managed", "all"}
            and not args.skip_managed
            and args.provider == "claude"
        ):
            results.append(profiler.run_managed_claude())
        elif (
            args.ownership in {"managed", "all"}
            and not args.skip_managed
            and args.provider == "cursor"
        ):
            results.append(profiler.run_managed_cursor())
        elif (
            args.ownership in {"managed", "all"}
            and not args.skip_managed
            and args.provider == "opencode"
        ):
            results.append(profiler.run_managed_opencode())
    except Exception as exc:
        errors.append(f"managed {args.provider} failed: {exc}")
        profiler.observe(
            case_id={"codex": "B1", "claude": "C1", "cursor": "D1"}.get(
                args.provider, "run"
            ),
            provider=args.provider,
            ownership="managed",
            source="harness",
            event="mismatch_detected",
            payload={"error": str(exc)},
        )
    try:
        if (
            args.ownership in {"unmanaged", "all"}
            and not args.skip_unmanaged
            and args.provider == "codex"
        ):
            results.append(profiler.run_unmanaged_codex())
        elif args.ownership in {"unmanaged", "all"} and not args.skip_unmanaged:
            raise RuntimeError(
                f"unmanaged {args.provider} profiling is not implemented"
            )
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
    return single_exit_code(
        errors=errors,
        metrics=metrics,
        sla_status=(profiler.sla_case or {}).get("status"),
    ), profiler.summary_path


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((pct / 100) * len(ordered)) - 1))
    return ordered[index]


def aggregate_batch_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    def metric_stats(source_cases: list[dict[str, Any]], key: str) -> dict[str, Any]:
        values = [
            value for case in source_cases if isinstance((value := case.get(key)), int)
        ]
        return {
            "count": len(values),
            "min": min(values) if values else None,
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "max": max(values) if values else None,
            "target": target_for_metric(key),
        }

    metrics = {key: metric_stats(cases, key) for key in BATCH_METRIC_KEYS}
    clean_cases = [case for case in cases if batch_case_is_clean(case)]
    metrics["clean_observation_count"] = len(clean_cases)
    metrics["clean_metrics"] = {
        key: metric_stats(clean_cases, key) for key in BATCH_METRIC_KEYS
    }
    return metrics


def batch_case_is_clean(case: dict[str, Any]) -> bool:
    """Return whether a case is eligible for clean distribution statistics."""

    return (
        case.get("verdict") in CLEAN_BATCH_VERDICTS
        and not case.get("failure_classification")
        and case.get("provider_timeout") is not True
    )


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


def single_exit_code(
    *, errors: list[str], metrics: list[dict[str, Any]], sla_status: str | None
) -> int:
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
            "## All observed metrics",
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
            "## Clean metrics",
            "",
            "Clean means verdict `pass` or `slow`, with no failure classification or provider timeout. "
            "Contaminated and incomplete observations remain in the all-observed table but are excluded here.",
            "",
            f"Clean observations: `{aggregate.get('clean_observation_count') or 0}`",
            "",
            "| Metric | Count | Min | P50 | P95 | Max | Target |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    clean_metrics = aggregate.get("clean_metrics") or {}
    for key in BATCH_METRIC_KEYS:
        item = clean_metrics.get(key) or {}
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


def local_transport_is_currently_healthy(data: dict[str, Any]) -> bool:
    transport = data.get("transport") or {}
    spool = data.get("spool") or {}
    if str(transport.get("status") or "") == "healthy":
        return int(spool.get("pending_count") or 0) == 0

    transport_health = data.get("transport_health") or {}
    return (
        str(transport_health.get("status") or "") != "offline"
        and transport_health.get("last_ship_result") == "ok"
        and int(transport_health.get("consecutive_failures") or 0) == 0
        and int(transport_health.get("spool_pending") or 0) == 0
    )


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
    outbox_stale = (
        isinstance(outbox_oldest_age, (int, float)) and outbox_oldest_age > 10
    )
    current_transport_ok = (
        local_transport_is_currently_healthy(data) and not outbox_stale
    )
    ok = (
        completed.returncode == 0
        and not outbox_stale
        and (health_state == "healthy" or current_transport_ok)
    )
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
        "transport": data.get("transport") or {},
        "outbox": outbox,
    }


def batch_local_health_preflight_with_grace(
    *, attempts: int = 6, retry_delay_seconds: float = 1.0
) -> dict[str, Any]:
    """Allow the engine's status file one bounded refresh window before aborting."""

    last: dict[str, Any] = {}
    for attempt in range(attempts):
        last = batch_local_health_preflight()
        if last.get("ok"):
            return last
        if (
            attempt + 1 < attempts
            and last.get("reason") == "local_transport_currently_unhealthy"
        ):
            time.sleep(retry_delay_seconds)
            continue
        return last
    return last


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
        health = batch_local_health_preflight_with_grace()
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
    exit_code = batch_exit_code(
        child_runs=child_runs, sla_status=(sla_case or {}).get("status")
    )
    batch_metrics_path = batch_dir / "batch-metrics.json"
    batch_metrics_path.write_text(
        json.dumps(
            {
                "schema_version": BATCH_METRICS_SCHEMA_VERSION,
                "batch_id": batch_id,
                "generated_at": utc_now(),
                "profile": args.profile,
                "profile_class": args.profile_class or profile_class_for(args.profile),
                "sla_case_id": args.sla_case
                or default_sla_case_id(args.profile, args.provider),
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
