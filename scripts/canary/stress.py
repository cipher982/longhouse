#!/usr/bin/env python3
"""One-shot stress profile for the realtime pipeline.

Fires RuntimeEventIngest posts at a target rate for a duration, then
scrapes /metrics and reports p50/p95/p99 latency from the delta buckets
captured during the test.

Usage:
    LONGHOUSE_CANARY_URL=... \
    LONGHOUSE_AGENTS_TOKEN=... \
    LONGHOUSE_CANARY_TOKEN=... \
    python3 scripts/canary/stress.py --rate 5 --duration 60

Uses a dedicated stress session_id (different from the steady-state canary)
so samples don't pollute the always-on baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx


def _runtime_event(session_id: str, seq: int, machine_name: str, now: datetime) -> dict:
    return {
        "runtime_key": f"canary-stress:{session_id}",
        "session_id": session_id,
        "provider": "canary",
        "device_id": machine_name,
        "source": "canary_stress",
        "kind": "progress_signal",
        "phase": None,
        "tool_name": None,
        "occurred_at": now.isoformat().replace("+00:00", "Z"),
        "dedupe_key": f"stress:{session_id}:{seq}",
        "payload": {"stress_seq": seq},
    }


def _parse_histogram(metrics_text: str, label_filter: str) -> list[tuple[float, float]]:
    out = []
    for line in metrics_text.splitlines():
        if not line.startswith("canary_latency_seconds_bucket"):
            continue
        if label_filter not in line:
            continue
        m = re.search(r'le="([^"]+)".*\}\s+([\d.eE+\-]+)', line)
        if not m:
            continue
        le = float("inf") if m.group(1) == "+Inf" else float(m.group(1))
        out.append((le, float(m.group(2))))
    out.sort()
    return out


def _delta(curr: list[tuple[float, float]], prev: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not prev or len(prev) != len(curr):
        return curr
    return [(le, max(0.0, c - p)) for (le, c), (_, p) in zip(curr, prev)]


def _pct(buckets: list[tuple[float, float]], p: float) -> float | None:
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    target = total * p
    for le, count in buckets:
        if count >= target:
            return le
    return buckets[-1][0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, required=True, help="Events per second")
    ap.add_argument("--duration", type=float, default=60.0, help="Total seconds to run")
    ap.add_argument("--label", default=None, help="Label appended to stress session name")
    args = ap.parse_args()

    base_url = os.environ["LONGHOUSE_CANARY_URL"].rstrip("/")
    agents_token = os.environ["LONGHOUSE_AGENTS_TOKEN"]

    machine = socket.gethostname()
    # Fire into the always-on canary session so the long-running observer
    # captures the SSE hop for us — no need to spawn a stress-local subscriber.
    session_file = Path(os.environ.get("LONGHOUSE_CANARY_SESSION_FILE", str(Path.home() / ".longhouse" / "canary-session-id")))
    if not session_file.exists():
        print(f"FATAL: {session_file} missing — producer must have run first", file=sys.stderr)
        return 2
    session_id = session_file.read_text().strip()

    print(f"stress: rate={args.rate}/s duration={args.duration}s session={session_id}")

    with httpx.Client(http2=True, timeout=15.0) as client:
        # Bootstrap session row (same as producer does).
        now = datetime.now(timezone.utc)
        ingest = {
            "id": session_id,
            "provider": "canary",
            "environment": "production",
            "project": f"canary-stress{'-' + args.label if args.label else ''}",
            "device_id": machine,
            "started_at": now.isoformat().replace("+00:00", "Z"),
            "events": [],
        }
        resp = client.post(f"{base_url}/api/agents/ingest", headers={"X-Agents-Token": agents_token}, json=ingest)
        if resp.status_code >= 300:
            print(f"bootstrap failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)

        # Snapshot canary histogram for the observer hop before test (used to
        # subtract baseline canary traffic from results).
        before_metrics = client.get(f"{base_url}/metrics").text
        before_buckets = _parse_histogram(before_metrics, 'hop="sse"')

        interval = 1.0 / args.rate
        end = time.monotonic() + args.duration
        seq = 0
        rtts_ms: list[int] = []
        errors = 0
        next_tick = time.monotonic()
        while time.monotonic() < end:
            seq += 1
            now = datetime.now(timezone.utc)
            payload = {"events": [_runtime_event(session_id, seq, machine, now)]}
            t0 = time.perf_counter()
            try:
                resp = client.post(
                    f"{base_url}/api/agents/runtime/events/batch",
                    headers={"X-Agents-Token": agents_token, "Content-Type": "application/json"},
                    json=payload,
                )
                rtts_ms.append(int((time.perf_counter() - t0) * 1000))
                if resp.status_code >= 300:
                    errors += 1
            except Exception:
                errors += 1
            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

        # Settle: give SSE observer up to 10s to drain in-flight observations.
        print("waiting 10s for SSE observer to drain...")
        time.sleep(10)

        after_metrics = client.get(f"{base_url}/metrics").text
        after_buckets = _parse_histogram(after_metrics, 'hop="sse"')
        delta = _delta(after_buckets, before_buckets)

    # Report
    rtts_ms.sort()
    n = len(rtts_ms)
    p50_rtt = rtts_ms[n // 2] if n else None
    p95_rtt = rtts_ms[int(n * 0.95)] if n else None
    p99_rtt = rtts_ms[int(n * 0.99)] if n else None

    sse_p50 = _pct(delta, 0.5)
    sse_p95 = _pct(delta, 0.95)
    sse_p99 = _pct(delta, 0.99)
    sse_total = delta[-1][1] if delta else 0

    print()
    print(f"rate: {args.rate}/s  duration: {args.duration}s  sent: {seq}  errors: {errors}")
    print(f"ingest RTT (cube → server round-trip): n={n} p50={p50_rtt}ms p95={p95_rtt}ms p99={p99_rtt}ms")
    print(f"SSE hop (server wake → observer): count_delta={int(sse_total)} p50≤{sse_p50}s p95≤{sse_p95}s p99≤{sse_p99}s")
    return 0 if errors == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
