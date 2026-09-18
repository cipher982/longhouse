#!/usr/bin/env python3
"""Trace one session's realtime path across every Longhouse subsystem.

Answers "how close to 1:1 realtime was this, and which hop was slow?" without
hand-writing SQL against the tenant DB each time. It prints one merged report:

  1. provider   — the transcript's own event timestamps (the ground truth)
  2. engine     — when the local Machine Agent saw the file change and shipped
  3. transport  — when the hosted instance committed each transcript chunk
  4. state      — the activity-fact / runtime-state plane that drives the UI badge
  5. served     — the exact facts the browser is handed right now
  6. verdict    — the slow hop, or "no gap found"

Run it on the machine that owns the session for sections 1-2; sections 3-5 come
from the Runtime Host over SSH. Everything is read-only.

Examples:
    python scripts/ops/session-realtime-trace.py --subdomain david010 --session <id>
    python scripts/ops/session-realtime-trace.py --subdomain david010 --session <id> \\
        --since 2026-09-18T01:10:00Z --until 2026-09-18T01:35:00Z --json

See the `managed-session-debug` skill for how to read the result.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

UTC = datetime.timezone.utc
DEFAULT_SSH_TARGET = os.environ.get("HOSTED_SESSION_DEBUG_SSH_TARGET", "zerg")
DEFAULT_HOST_DATA_ROOT = "/var/app-data/longhouse"
DEVICE_TOKEN_PATH = Path.home() / ".longhouse" / "machine" / "device-token"
ENGINE_LOG_DIR = Path.home() / ".longhouse" / "agent" / "logs"
SHIPPER_DB = Path.home() / ".longhouse" / "agent" / "longhouse-shipper.db"
CHANNEL_STATE_DIR = Path.home() / ".claude" / "channels" / "longhouse" / "sessions"

# Transport lag above this is a real gap rather than the normal event-to-ship
# debounce; the SLA manifest targets 3s for local-to-durable.
TRANSPORT_LAG_WARN_S = 10.0
STATE_STALE_WARN_S = 60.0

ENGINE_LINE = re.compile(
    r"^(?P<ts>\S+Z)\s+(?P<level>\w+)\s+engine\.path_job\{job=PathJob \{ path: \"(?P<path>[^\"]+)\".*?"
    r"observation: ObservationTrace \{ source: \"(?P<source>[^\"]*)\".*?"
    r"observed_at_ms: (?P<observed_ms>\d+).*?enqueued_at_ms: (?P<enqueued_ms>\d+)"
    r".*?longhouse_engine::daemon: (?P<message>.+)$"
)


def parse_iso(value: str) -> datetime.datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(value: datetime.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_db_ts(value: object) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(float(value) / 1_000_000, UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def microseconds(value: object) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    return datetime.datetime.fromtimestamp(int(value) / 1_000_000, UTC)


def milliseconds(value: object) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    return datetime.datetime.fromtimestamp(int(value) / 1_000, UTC)


def run(cmd: list[str], *, timeout: int = 120, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)


def resolve_provider_session(session_id: str) -> dict[str, object]:
    """Read the local Claude channel state for this managed session, if present."""
    state_path = CHANNEL_STATE_DIR / f"{session_id}.json"
    if not state_path.is_file():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def find_transcript(provider_session_id: str, cwd: str | None) -> Path | None:
    if not provider_session_id:
        return None
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None
    if cwd:
        candidate = projects / re.sub(r"[/.]", "-", cwd) / f"{provider_session_id}.jsonl"
        if candidate.is_file():
            return candidate
    for candidate in projects.glob(f"*/{provider_session_id}.jsonl"):
        return candidate
    return None


def read_transcript_events(path: Path, since: datetime.datetime, until: datetime.datetime) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for line in handle:
            if '"timestamp":"2026' not in line and '"timestamp": "2026' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            stamp = parse_db_ts(record.get("timestamp"))
            if stamp is None or not (since <= stamp <= until):
                continue
            message = record.get("message") or {}
            content = message.get("content")
            kinds: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "tool_use":
                        kinds.append(f"tool_use:{block.get('name')}")
                    elif block_type == "tool_result":
                        kinds.append("tool_result")
                    elif block_type in {"text", "thinking"}:
                        kinds.append(block_type)
            elif isinstance(content, str):
                kinds.append("text")
            if not kinds or set(kinds) == {"attachment"}:
                continue
            rows.append({"at": iso(stamp), "role": record.get("type"), "parts": kinds})
    return rows


def read_engine_ships(transcript: Path | None, since: datetime.datetime, until: datetime.datetime) -> list[dict[str, object]]:
    if transcript is None or not ENGINE_LOG_DIR.is_dir():
        return []
    rows: list[dict[str, object]] = []
    day = since.date()
    while day <= until.date():
        log_path = ENGINE_LOG_DIR / f"engine.log.{day.isoformat()}"
        day += datetime.timedelta(days=1)
        if not log_path.is_file():
            continue
        with log_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if str(transcript) not in line:
                    continue
                match = ENGINE_LINE.match(line.rstrip("\n"))
                if not match:
                    continue
                shipped_at = parse_db_ts(match.group("ts"))
                if shipped_at is None or not (since <= shipped_at <= until):
                    continue
                observed = milliseconds(int(match.group("observed_ms")))
                enqueued = milliseconds(int(match.group("enqueued_ms")))
                message = (match.group("message") or "").split(" path=")[0].strip()
                bytes_match = re.search(r"bytes_shipped=(\d+)", line)
                events_match = re.search(r"events_shipped=(\d+)", line)
                rows.append(
                    {
                        "at": iso(shipped_at),
                        "outcome": message,
                        "source": match.group("source"),
                        "observed_at": iso(observed) if observed else None,
                        "enqueued_at": iso(enqueued) if enqueued else None,
                        "queue_ms": int((enqueued - observed).total_seconds() * 1000) if observed and enqueued else None,
                        "ship_ms": int((shipped_at - enqueued).total_seconds() * 1000) if enqueued else None,
                        "bytes": int(bytes_match.group(1)) if bytes_match else None,
                        "events": int(events_match.group(1)) if events_match else None,
                    }
                )
    return rows


REMOTE_PROBE = r"""
import datetime, json, sqlite3, sys

db_path, session_id, since, until = sys.argv[1:5]
con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20)
con.row_factory = sqlite3.Row
cur = con.cursor()
out = {}


def scalar(sql, args=()):
    try:
        return cur.execute(sql, args).fetchone()
    except Exception as exc:  # pragma: no cover - diagnostic path
        return {"error": str(exc)}


def rows(sql, args=()):
    try:
        return [dict(r) for r in cur.execute(sql, args).fetchall()]
    except Exception as exc:  # pragma: no cover - diagnostic path
        return [{"error": str(exc)}]


out["sessions"] = rows("SELECT session_id, started_at, ended_at, commit_seq, updated_at, tool_calls FROM sessions WHERE session_id=?", (session_id,))
out["siblings"] = rows(
    "SELECT session_id, started_at, ended_at, created_at FROM sessions "
    "WHERE started_at = (SELECT started_at FROM sessions WHERE session_id=?) AND session_id <> ?",
    (session_id, session_id),
)
out["runtime_state"] = rows("SELECT * FROM live_runtime_state WHERE session_id=?", (session_id,))
out["interactions"] = rows(
    "SELECT id, kind, status, can_respond, response_text, occurred_at, resolved_at FROM live_interaction_requests WHERE session_id=?",
    (session_id,),
)
out["heads"] = rows(
    "SELECT family, subject_key, source, observed_at, valid_until, updated_commit_seq FROM fact_heads WHERE value_json LIKE ?",
    (f"%{session_id}%",),
)
out["chunks"] = rows(
    "SELECT event_count, tool_calls, first_order_time_us, last_order_time_us, commit_seq, created_at "
    "FROM render_objects WHERE session_id=? AND datetime(created_at) >= datetime(?) AND datetime(created_at) <= datetime(?) "
    "ORDER BY created_at ASC",
    (session_id, since, until),
)
out["chunk_extent"] = rows(
    "SELECT COUNT(*) AS objects, MIN(created_at) AS first_created_at, MAX(created_at) AS last_created_at "
    "FROM render_objects WHERE session_id=?",
    (session_id,),
)
run_row = scalar(
    "SELECT subject_key FROM fact_heads WHERE family='activity' AND value_json LIKE ? ORDER BY updated_commit_seq DESC LIMIT 1",
    (f"%{session_id}%",),
)
run_key = dict(run_row)["subject_key"] if run_row and "subject_key" in run_row.keys() else None
out["run_key"] = run_key
if run_key:
    out["activity_receipts"] = rows(
        "SELECT position_key, received_at, commit_seq FROM fact_receipts "
        "WHERE family='activity' AND subject_key=? AND datetime(received_at) >= datetime(?) AND datetime(received_at) <= datetime(?) "
        "ORDER BY received_at ASC",
        (run_key, since, until),
    )
else:
    out["activity_receipts"] = []
out["activity_receipt_extent"] = rows(
    "SELECT COUNT(*) AS rows_retained, MIN(received_at) AS first_received_at, MAX(received_at) AS last_received_at "
    "FROM fact_receipts WHERE family='activity' AND subject_key=?",
    (run_key,),
) if run_key else []
print(json.dumps(out, default=str))
"""


def fetch_remote(ssh_target: str, db_path: str, session_id: str, since: str, until: str) -> dict[str, object]:
    result = run(
        ["ssh", ssh_target, "python3 -", db_path, session_id, since, until],
        stdin=REMOTE_PROBE,
        timeout=300,
    )
    if result.returncode != 0:
        return {"error": (result.stderr or result.stdout or "").strip()[:500]}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": f"remote probe returned non-JSON output: {result.stdout[:200]!r}"}


def fetch_served(runtime_url: str, session_id: str) -> dict[str, object]:
    token = os.environ.get("LONGHOUSE_AGENTS_TOKEN", "").strip()
    if not token and DEVICE_TOKEN_PATH.is_file():
        token = DEVICE_TOKEN_PATH.read_text().strip()
    if not token:
        return {"error": "no agents token (set LONGHOUSE_AGENTS_TOKEN or install a machine device token)"}
    request = urllib.request.Request(
        f"{runtime_url.rstrip('/')}/api/agents/sessions/{session_id}/workspace?limit=1&branch_mode=head",
        headers={"X-Agents-Token": token, "User-Agent": "longhouse-session-realtime-trace/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def build_chunk_lags(chunks: list[dict[str, object]]) -> list[dict[str, object]]:
    lags: list[dict[str, object]] = []
    for chunk in chunks:
        created = parse_db_ts(chunk.get("created_at"))
        last = microseconds(chunk.get("last_order_time_us"))
        first = microseconds(chunk.get("first_order_time_us"))
        lags.append(
            {
                "created_at": iso(created) if created else None,
                "first_event_at": iso(first) if first else None,
                "last_event_at": iso(last) if last else None,
                "lag_s": round((created - last).total_seconds(), 2) if created and last else None,
                "span_s": round((last - first).total_seconds(), 2) if first and last else None,
                "events": chunk.get("event_count"),
                "tools": chunk.get("tool_calls"),
            }
        )
    return lags


def verdict(report: dict[str, object]) -> list[str]:
    lines: list[str] = []
    lags = [entry["lag_s"] for entry in report["chunk_lags"] if entry["lag_s"] is not None]
    if lags:
        slowest = max(report["chunk_lags"], key=lambda entry: entry["lag_s"] or 0.0)
        lines.append(
            "transport: provider-event -> hosted-commit p50 {:.1f}s / p95 {:.1f}s / max {:.1f}s across {} chunks".format(
                percentile(lags, 0.5) or 0.0,
                percentile(lags, 0.95) or 0.0,
                max(lags),
                len(lags),
            )
        )
        if max(lags) >= TRANSPORT_LAG_WARN_S:
            lines.append(
                f"transport: worst chunk is {slowest['lag_s']}s "
                f"(last provider event {slowest['last_event_at']}, committed {slowest['created_at']})"
            )
    else:
        lines.append("transport: no hosted chunks in the window")

    ships = report.get("engine_ships") or []
    queue_ms = [entry["queue_ms"] for entry in ships if entry.get("queue_ms") is not None]
    if queue_ms:
        lines.append(
            "engine: observation -> enqueue p50 {:.0f}ms / max {:.0f}ms across {} ships".format(
                percentile([float(v) for v in queue_ms], 0.5) or 0.0,
                float(max(queue_ms)),
                len(queue_ms),
            )
        )

    served = report.get("served") or {}
    state = (served.get("session") or {}).get("session_state") or {}
    activity = state.get("activity") or {}
    observed = parse_db_ts(activity.get("observed_at"))
    now = report["window"]["generated_at_dt"]
    if observed:
        age = (now - observed).total_seconds()
        lines.append(
            "state plane: served activity={} raw_kind={} source={} observed {}s ago".format(
                activity.get("state"), activity.get("raw_kind"), activity.get("source"), int(age)
            )
        )
        if age >= STATE_STALE_WARN_S:
            lines.append(
                "state plane: the served badge is not derived from the transcript; it is a "
                f"{int(age)}s-old provider hook observation, so the header can disagree with the rows below it"
            )
    else:
        lines.append("state plane: no served activity facts")
    return lines


def render_text(report: dict[str, object]) -> None:
    window = report["window"]
    print("=" * 78)
    print(f"session {report['session_id']}  subdomain {report['subdomain']}")
    print(f"window  {window['since']} -> {window['until']}   (generated {window['generated_at']})")
    print("=" * 78)

    provider = report.get("provider") or {}
    print("\n## provider (Claude transcript)")
    if provider.get("error"):
        print(f"  unavailable: {provider['error']}")
    else:
        print(f"  transcript: {provider.get('transcript')}")
        for row in provider.get("events", []):
            print(f"  {row['at']}  {row['role']:<9} {'|'.join(row['parts']) or '-'}")

    engine = report.get("engine") or {}
    print("\n## engine (Machine Agent shipping)")
    if engine.get("error"):
        print(f"  unavailable: {engine['error']}")
    else:
        for row in engine.get("ships", []):
            print(
                f"  {row['at']}  {row['outcome']:<32} source={row['source']:<18}"
                f" queue={row['queue_ms']}ms ship={row['ship_ms']}ms bytes={row['bytes']} events={row['events']}"
            )
        ledger = engine.get("phase_ledger") or {}
        if ledger:
            print(f"  local phase ledger: {json.dumps(ledger)}")

    print("\n## transport (hosted chunk commits)")
    for row in report.get("chunk_lags", []):
        print(
            f"  committed {row['created_at']}  events {row['events']:>3}  tools {row['tools']:>2}"
            f"  last provider event {row['last_event_at'] or '-'}  lag {row['lag_s'] if row['lag_s'] is not None else '-'}s"
        )

    remote = report.get("hosted") or {}
    print("\n## state plane (activity facts + interaction requests)")
    if remote.get("error"):
        print(f"  unavailable: {remote['error']}")
    else:
        runtime = (remote.get("runtime_state") or [{}])[0]
        print(
            "  live_runtime_state: phase={} source={} tool={} pending_interaction={}".format(
                runtime.get("phase"),
                runtime.get("phase_source"),
                runtime.get("active_tool"),
                runtime.get("pending_interaction_id"),
            )
        )
        for row in remote.get("heads", []):
            print(
                f"  head {row['family']:<12} {row['subject_key'][:44]:<44} source={row['source']:<22}"
                f" observed={row['observed_at']} valid_until={row['valid_until']}"
            )
        for row in remote.get("activity_receipts", []):
            print(f"  activity receipt  observed {row['position_key']}  committed {row['received_at']}")
        if not remote.get("activity_receipts"):
            extent = (remote.get("activity_receipt_extent") or [{}])[0]
            print(
                "  activity receipts: none in this window — fact_receipts keeps only this subject's most recent "
                f"{extent.get('rows_retained')} rows ({extent.get('first_received_at')} .. {extent.get('last_received_at')}); "
                "use the local phase ledger and engine ships below for older windows"
            )
        for row in remote.get("interactions", []):
            print(f"  interaction {row['kind']} status={row['status']} resolved_at={row['resolved_at']}")
        for row in remote.get("siblings", []):
            print(f"  sibling session row {row['session_id']} started={row['started_at']} ended={row['ended_at']}")

    served = report.get("served") or {}
    print("\n## served (what the browser is handed)")
    if served.get("error"):
        print(f"  unavailable: {served['error']}")
    else:
        state = (served.get("session") or {}).get("session_state") or {}
        print(f"  presentation.primary: {json.dumps(state.get('presentation', {}).get('primary'))}")
        print(f"  activity: {json.dumps(state.get('activity'))}")
        print(f"  pending_interaction: {json.dumps(state.get('pending_interaction'))}")
        print(f"  transcript: {json.dumps(state.get('transcript'))}")

    print("\n## verdict")
    for line in report.get("verdict", []):
        print(f"  {line}")
    print()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, help="managed Longhouse session id")
    parser.add_argument("--subdomain", default=os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN", ""))
    parser.add_argument("--runtime-url", default="", help="override the Runtime Host base URL")
    parser.add_argument("--ssh", default=DEFAULT_SSH_TARGET, help=f"Runtime Host ssh target (default {DEFAULT_SSH_TARGET})")
    parser.add_argument("--db-path", default="", help="tenant catalog DB path on the host")
    parser.add_argument("--since", default="", help="ISO window start (default: 30 minutes before --until)")
    parser.add_argument("--until", default="", help="ISO window end (default: now)")
    parser.add_argument("--json", action="store_true", help="emit the raw report instead of text")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.subdomain and not args.runtime_url:
        print("Need --subdomain or --runtime-url", file=sys.stderr)
        return 2

    until = parse_iso(args.until) if args.until else datetime.datetime.now(UTC)
    since = parse_iso(args.since) if args.since else until - datetime.timedelta(minutes=30)
    runtime_url = args.runtime_url or f"https://{args.subdomain}.longhouse.ai"
    db_path = args.db_path or f"{DEFAULT_HOST_DATA_ROOT}/{args.subdomain}/longhouse-live.db"

    channel = resolve_provider_session(args.session)
    transcript = find_transcript(str(channel.get("provider_session_id") or ""), str(channel.get("cwd") or ""))

    provider: dict[str, object]
    if transcript is None:
        provider = {"error": "transcript not on this machine (run the provider/engine section where the session runs)"}
    else:
        provider = {"transcript": str(transcript), "events": read_transcript_events(transcript, since, until)}

    ledger: dict[str, object] = {}
    if SHIPPER_DB.is_file():
        import sqlite3

        connection = sqlite3.connect(f"file:{SHIPPER_DB}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT phase, tool_name, source, observed_at FROM session_phase_state WHERE session_id=?",
                (args.session,),
            ).fetchone()
            if row:
                ledger = dict(row)
        finally:
            connection.close()

    hosted = fetch_remote(args.ssh, db_path, args.session, iso(since), iso(until))

    report: dict[str, object] = {
        "session_id": args.session,
        "subdomain": args.subdomain or runtime_url,
        "window": {
            "since": iso(since),
            "until": iso(until),
            "generated_at": iso(datetime.datetime.now(UTC)),
            "generated_at_dt": datetime.datetime.now(UTC),
        },
        "provider": provider,
        "engine": {
            "ships": read_engine_ships(transcript, since, until),
            "phase_ledger": ledger,
        },
        "hosted": hosted,
        "chunk_lags": build_chunk_lags(hosted.get("chunks") or []),
        "served": fetch_served(runtime_url, args.session),
    }
    report["verdict"] = verdict(report)

    if args.json:
        report["window"].pop("generated_at_dt", None)
        print(json.dumps(report, indent=2, default=str))
    else:
        render_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
