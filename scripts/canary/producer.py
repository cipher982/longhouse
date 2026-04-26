#!/usr/bin/env python3
"""Longhouse realtime canary producer.

Every INTERVAL seconds, POST a fabricated RuntimeEventIngest to
/api/agents/runtime/events/batch, stamped with a monotonic canary_seq +
emitted_at=now(). Measure server receive latency (round-trip minus a
best-effort network half) and POST a CanaryObservation back.

Runs forever. Re-uses the same canary session_id across restarts so all
probes aggregate into one session on the server.

Usage:
    LONGHOUSE_CANARY_URL=https://david010.longhouse.ai \
    LONGHOUSE_CANARY_TOKEN=<device-token> \
    LONGHOUSE_CANARY_ADMIN_COOKIE='longhouse_session=...' \
    python3 scripts/canary/producer.py
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sys
import time
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx

INTERVAL_S = float(os.environ.get("LONGHOUSE_CANARY_INTERVAL_S", "30"))
SESSION_ID_FILE = Path(os.environ.get("LONGHOUSE_CANARY_SESSION_FILE", str(Path.home() / ".longhouse" / "canary-session-id")))
SEQ_FILE = SESSION_ID_FILE.with_name("canary-seq")


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"FATAL: missing {key}", file=sys.stderr)
        sys.exit(2)
    return value


def _session_uuid() -> str:
    """Stable UUID per machine so restarts aggregate into one session."""
    if SESSION_ID_FILE.exists():
        return SESSION_ID_FILE.read_text().strip()
    # Deterministic-ish: machine name + install time seeds a v4.
    seed = socket.gethostname().encode("utf-8") + str(int(time.time())).encode("utf-8")
    h = hashlib.sha256(seed).digest()
    sid = str(uuid.UUID(bytes=h[:16], version=4))
    SESSION_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_ID_FILE.write_text(sid)
    return sid


def _next_seq() -> int:
    try:
        current = int(SEQ_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        current = 0
    nxt = current + 1
    SEQ_FILE.write_text(str(nxt))
    return nxt


def _runtime_event(session_id: str, seq: int, machine_name: str, now: datetime) -> dict:
    return {
        "runtime_key": f"canary:{session_id}",
        "session_id": session_id,
        "provider": "canary",
        "device_id": machine_name,
        "source": "canary_producer",
        "kind": "progress_signal",
        "phase": None,
        "tool_name": None,
        "occurred_at": now.isoformat().replace("+00:00", "Z"),
        "dedupe_key": f"canary:{session_id}:{seq}",
        "payload": {"canary_seq": seq, "canary_emitted_at_ms": int(now.timestamp() * 1000)},
    }


def _binding_event(session_id: str, machine_name: str, now: datetime) -> dict:
    return {
        "runtime_key": f"canary:{session_id}",
        "session_id": session_id,
        "provider": "canary",
        "device_id": machine_name,
        "source": "canary_producer",
        "kind": "binding_signal",
        "phase": None,
        "tool_name": None,
        "occurred_at": now.isoformat().replace("+00:00", "Z"),
        "dedupe_key": f"canary:{session_id}:binding",
        "payload": {"canary_bootstrap": True},
    }


def _post_observation(
    client: httpx.Client,
    base_url: str,
    admin_cookie: str,
    *,
    canary_seq: int,
    hop: str,
    surface: str,
    latency_ms: int,
) -> None:
    try:
        resp = client.post(
            f"{base_url}/api/telemetry/canary-observation",
            headers={"Cookie": admin_cookie, "Content-Type": "application/json"},
            json={
                "canary_seq": canary_seq,
                "hop": hop,
                "surface": surface,
                "latency_ms": max(0, int(latency_ms)),
            },
            timeout=10.0,
        )
        if resp.status_code >= 300:
            print(f"observation post {hop} seq={canary_seq} -> {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"observation post error: {exc}", file=sys.stderr)


def main() -> int:
    base_url = _require_env("LONGHOUSE_CANARY_URL").rstrip("/")
    token = _require_env("LONGHOUSE_CANARY_TOKEN")
    admin_cookie = os.environ.get("LONGHOUSE_CANARY_ADMIN_COOKIE", "")
    machine_name = os.environ.get("LONGHOUSE_CANARY_MACHINE", socket.gethostname())
    session_id = _session_uuid()

    print(f"canary producer: session_id={session_id}, interval={INTERVAL_S}s, target={base_url}")

    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    with httpx.Client(http2=True) as client:
        # Bootstrap: binding_signal so the session exists.
        now = datetime.now(timezone.utc)
        bootstrap = {"events": [_binding_event(session_id, machine_name, now)]}
        try:
            resp = client.post(
                f"{base_url}/api/agents/runtime/events/batch",
                headers={"X-Agents-Token": token, "Content-Type": "application/json"},
                json=bootstrap,
                timeout=15.0,
            )
            if resp.status_code >= 300:
                print(f"bootstrap failed {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
                return 3
        except Exception as exc:
            print(f"bootstrap network error: {exc}", file=sys.stderr)
            return 3

        while not stopping:
            seq = _next_seq()
            emitted_at = datetime.now(timezone.utc)
            payload = {"events": [_runtime_event(session_id, seq, machine_name, emitted_at)]}
            send_start = time.perf_counter()
            try:
                resp = client.post(
                    f"{base_url}/api/agents/runtime/events/batch",
                    headers={"X-Agents-Token": token, "Content-Type": "application/json"},
                    json=payload,
                    timeout=15.0,
                )
                rtt_ms = int((time.perf_counter() - send_start) * 1000)
                if resp.status_code >= 300:
                    print(f"ingest post seq={seq} -> {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                else:
                    # Record hop=ingest as one-way latency ≈ RTT/2.
                    # Honest note: we can't separate send from ack cleanly without
                    # server echoing our emitted_at; server's event_age_at_ingest
                    # already measures that properly. The `hop=ingest` canary
                    # observation is a liveness signal, not an SLA source.
                    if admin_cookie:
                        _post_observation(
                            client,
                            base_url,
                            admin_cookie,
                            canary_seq=seq,
                            hop="ingest",
                            surface="producer",
                            latency_ms=rtt_ms,
                        )
            except Exception as exc:
                print(f"ingest network error: {exc}", file=sys.stderr)

            # Sleep in small steps so SIGTERM is responsive.
            slept = 0.0
            while slept < INTERVAL_S and not stopping:
                time.sleep(min(0.5, INTERVAL_S - slept))
                slept += 0.5

    print("canary producer stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
