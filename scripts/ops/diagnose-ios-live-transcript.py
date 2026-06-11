#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOME = Path.home() / ".longhouse"
TAIL_BYTES = 128 * 1024


def tail_text(path: Path, max_bytes: int = TAIL_BYTES) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        return fh.read(max_bytes).decode("utf-8", errors="replace")


def bridge_log_health(log_path: Path) -> dict[str, Any]:
    health: dict[str, Any] = {
        "log_file": str(log_path),
        "exists": log_path.exists(),
        "status": "unknown",
        "retry_count": 0,
        "network_error_count": 0,
        "failed_count": 0,
        "dropped_count": 0,
        "cloudflare_502_count": 0,
        "slow_count": 0,
        "slow_max_elapsed_ms": None,
        "slow_max_queue_wait_ms": None,
    }
    text = tail_text(log_path)
    if not text:
        return health
    for line in text.splitlines():
        if "live runtime ingest retrying" in line:
            health["retry_count"] += 1
        if "runtime ingest network error" in line:
            health["network_error_count"] += 1
        if "runtime ingest failed" in line:
            health["failed_count"] += 1
        if "live runtime ingest dropped" in line:
            health["dropped_count"] += 1
        if "Error 502" in line or "Bad gateway" in line or "Cloudflare Ray ID" in line:
            health["cloudflare_502_count"] += 1
        if "live runtime ingest slow" in line:
            health["slow_count"] += 1
            update_max(health, "slow_max_elapsed_ms", extract_number(line, "elapsed_ms"))
            update_max(health, "slow_max_queue_wait_ms", extract_number(line, "queue_wait_ms"))

    terminal = (
        health["network_error_count"]
        + health["failed_count"]
        + health["dropped_count"]
        + health["cloudflare_502_count"]
    )
    if terminal:
        health["status"] = "broken"
    elif health["retry_count"] or health["slow_count"]:
        health["status"] = "degraded"
    else:
        health["status"] = "healthy"
    return health


def extract_number(line: str, name: str) -> float | None:
    marker = f"{name}="
    start = line.find(marker)
    if start < 0:
        return None
    raw = line[start + len(marker) :]
    if raw.startswith("Some("):
        raw = raw[5:]
    number = []
    for ch in raw:
        if ch.isdigit() or ch == ".":
            number.append(ch)
        else:
            break
    if not number:
        return None
    return float("".join(number))


def update_max(row: dict[str, Any], key: str, value: float | None) -> None:
    if value is None:
        return
    current = row.get(key)
    if current is None or value > float(current):
        row[key] = value


def read_engine_status(home: Path) -> dict[str, Any]:
    path = home / "agent" / "engine-status.json"
    if not path.exists():
        return {"path": str(path), "exists": False}
    payload = json.loads(path.read_text())
    live = dict(dict(payload.get("ship_lanes") or {}).get("live") or {})
    return {
        "path": str(path),
        "exists": True,
        "version": payload.get("version"),
        "live": {
            "attempts_1h": live.get("attempts_1h"),
            "successes_1h": live.get("successes_1h"),
            "events_1h": live.get("events_1h"),
            "latency_p50_ms_1h": live.get("latency_p50_ms_1h"),
            "latency_p95_ms_1h": live.get("latency_p95_ms_1h"),
            "stage_latency_p95_ms_1h": live.get("stage_latency_p95_ms_1h"),
            "last_attempt_at": live.get("last_attempt_at"),
            "last_success_at": live.get("last_success_at"),
        },
    }


def recent_engine_lines(home: Path, session_id: str, limit: int) -> list[str]:
    log_dir = home / "agent" / "logs"
    if not log_dir.exists():
        return []
    candidates = sorted(log_dir.glob("engine.log*"), key=lambda p: p.stat().st_mtime)
    lines: list[str] = []
    for path in candidates[-3:]:
        text = tail_text(path, max_bytes=512 * 1024)
        for line in text.splitlines():
            if session_id in line or ("Shipped transcript path" in line and 'provider="codex"' in line):
                lines.append(line)
    return lines[-limit:]


def hosted_debug(session_id: str, subdomain: str, limit: int) -> dict[str, Any]:
    cmd = [
        str(ROOT / "scripts" / "ops" / "hosted-session-debug.sh"),
        "--subdomain",
        subdomain,
        "--session",
        session_id,
        "--limit",
        str(limit),
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        return {
            "ok": False,
            "exit_code": proc.returncode,
            "stderr": proc.stderr.strip(),
            "stdout": proc.stdout.strip(),
        }
    try:
        return {"ok": True, "payload": json.loads(proc.stdout)}
    except json.JSONDecodeError:
        return {"ok": False, "exit_code": proc.returncode, "stdout": proc.stdout.strip()}


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    home = Path(args.longhouse_home).expanduser()
    session_id = args.session
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "session_id": session_id,
        "engine_status": read_engine_status(home),
        "bridge_live_runtime_ingest": bridge_log_health(
            home / "managed-local" / "codex-bridge" / f"{session_id}.log"
        ),
        "recent_engine_lines": recent_engine_lines(home, session_id, args.engine_line_limit),
        "hosted": hosted_debug(session_id, args.subdomain, args.hosted_limit) if args.hosted else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose local-to-iOS live transcript freshness for one session.")
    parser.add_argument("--session", required=True, help="Longhouse session id to inspect.")
    parser.add_argument("--subdomain", default="david010", help="Hosted tenant subdomain for optional hosted debug.")
    parser.add_argument("--longhouse-home", default=str(DEFAULT_HOME), help="Longhouse home, defaults to ~/.longhouse.")
    parser.add_argument("--engine-line-limit", type=int, default=12, help="Recent engine log lines to include.")
    parser.add_argument("--hosted-limit", type=int, default=10, help="Rows per hosted debug section.")
    parser.add_argument("--hosted", action="store_true", help="Also run hosted-session-debug.sh.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a concise text report.")
    args = parser.parse_args()

    report = build_report(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    live = dict(dict(report["engine_status"]).get("live") or {})
    bridge = dict(report["bridge_live_runtime_ingest"])
    print(f"Session: {report['session_id']}")
    print(f"Engine build: {report['engine_status'].get('version') or '-'}")
    print(
        "Engine live lane: "
        f"{live.get('successes_1h', 0)}/{live.get('attempts_1h', 0)} ok, "
        f"p50={live.get('latency_p50_ms_1h') or '-'}ms, "
        f"p95={live.get('latency_p95_ms_1h') or '-'}ms"
    )
    print(
        "Bridge live ingest: "
        f"{bridge.get('status')}; retries={bridge.get('retry_count', 0)}, "
        f"network={bridge.get('network_error_count', 0)}, "
        f"failed={bridge.get('failed_count', 0)}, "
        f"dropped={bridge.get('dropped_count', 0)}, "
        f"cf502={bridge.get('cloudflare_502_count', 0)}, "
        f"slow={bridge.get('slow_count', 0)}"
    )
    if report["recent_engine_lines"]:
        print("Recent engine lines:")
        for line in report["recent_engine_lines"]:
            print(f"  {line}")
    if report["hosted"] is not None:
        hosted = dict(report["hosted"])
        print(f"Hosted debug: {'ok' if hosted.get('ok') else 'failed'}")
        if not hosted.get("ok"):
            print(f"  {hosted.get('stderr') or hosted.get('stdout') or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
