#!/usr/bin/env python3
"""Longhouse realtime canary observer.

Subscribes to the canary session's workspace SSE stream, measures server
wake latency by comparing the frame's latest_event_emitted_at_ms against
wall-clock arrival, and POSTs CanaryObservation to the admin endpoint.

The producer must be running first so a canary session exists and
~/.longhouse/canary-session-id is populated.

Usage:
    LONGHOUSE_CANARY_URL=https://your-instance.longhouse.ai \
    LONGHOUSE_CANARY_TOKEN=<shared-secret-set-on-server> \
    python3 scripts/canary/observer.py

Exits non-zero if SSE is unreachable for > UNREACHABLE_TIMEOUT_S.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx

SESSION_ID_FILE = Path(os.environ.get("LONGHOUSE_CANARY_SESSION_FILE", str(Path.home() / ".longhouse" / "canary-session-id")))
UNREACHABLE_TIMEOUT_S = int(os.environ.get("LONGHOUSE_CANARY_UNREACHABLE_S", "300"))


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"FATAL: missing {key}", file=sys.stderr)
        sys.exit(2)
    return value


def _post_observation(
    client: httpx.Client,
    base_url: str,
    canary_token: str,
    *,
    canary_seq: int,
    hop: str,
    surface: str,
    latency_ms: int,
) -> None:
    try:
        resp = client.post(
            f"{base_url}/api/telemetry/canary-observation",
            headers={"X-Canary-Token": canary_token, "Content-Type": "application/json"},
            json={
                "canary_seq": canary_seq,
                "hop": hop,
                "surface": surface,
                "latency_ms": max(0, int(latency_ms)),
            },
            timeout=10.0,
        )
        if resp.status_code >= 300:
            print(f"obs post {hop} seq={canary_seq} -> {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"obs post error: {exc}", file=sys.stderr)


def _iter_sse(response: httpx.Response):
    """Yield (event, data) tuples from a text/event-stream response."""
    event_name = ""
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)


def main() -> int:
    base_url = _require_env("LONGHOUSE_CANARY_URL").rstrip("/")
    canary_token = _require_env("LONGHOUSE_CANARY_TOKEN")
    if not SESSION_ID_FILE.exists():
        print(f"FATAL: {SESSION_ID_FILE} not found — start the producer first", file=sys.stderr)
        return 2

    session_id = SESSION_ID_FILE.read_text().strip()
    stream_url = f"{base_url}/api/canary/sessions/{session_id}/workspace/stream"
    print(f"canary observer: session_id={session_id} stream={stream_url}")

    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    last_successful_read_at = time.monotonic()
    backoff_s = 1.0

    while not stopping:
        try:
            with httpx.Client(http2=False, timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)) as client:
                with client.stream(
                    "GET",
                    stream_url,
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "X-Canary-Token": canary_token,
                    },
                ) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"SSE HTTP {response.status_code}: {response.text[:200]}")
                    backoff_s = 1.0
                    for event_name, payload in _iter_sse(response):
                        last_successful_read_at = time.monotonic()
                        if stopping:
                            break
                        if event_name != "workspace_changed":
                            continue
                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        # Prefer provider emitted_at when available (real user
                        # surface). Canary sessions don't write AgentEvent rows,
                        # so emitted_at is null; fall back to server_now_ms which
                        # still captures the SSE-send + network path.
                        emitted_ms = data.get("latest_event_emitted_at_ms") or data.get("server_now_ms")
                        if not emitted_ms:
                            continue
                        canary_seq = data.get("pubsub_seq") or 0
                        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                        latency_ms = max(0, now_ms - int(emitted_ms))
                        # Skip absurdly stale wakes — they mean the frame is a
                        # resubscribe replay, not a realtime push.
                        if latency_ms > 60_000:
                            continue
                        _post_observation(
                            client,
                            base_url,
                            canary_token,
                            canary_seq=int(canary_seq),
                            hop="sse",
                            surface="observer",
                            latency_ms=latency_ms,
                        )
        except Exception as exc:
            print(f"SSE error ({exc.__class__.__name__}): {exc}", file=sys.stderr)
            if time.monotonic() - last_successful_read_at > UNREACHABLE_TIMEOUT_S:
                print(f"SSE unreachable > {UNREACHABLE_TIMEOUT_S}s; exiting for supervisor restart", file=sys.stderr)
                return 3
            # Exponential backoff capped at 30s.
            slept = 0.0
            while slept < backoff_s and not stopping:
                time.sleep(min(0.5, backoff_s - slept))
                slept += 0.5
            backoff_s = min(30.0, backoff_s * 2)

    print("canary observer stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
