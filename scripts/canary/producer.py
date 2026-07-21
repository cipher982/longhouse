#!/usr/bin/env python3
"""Longhouse realtime canary producer.

Every INTERVAL seconds, POST a fabricated RuntimeEventIngest to
/api/agents/runtime/events/batch, stamped with a monotonic canary_seq +
emitted_at=now(). Measure server receive latency (round-trip minus a
best-effort network half) and POST a CanaryObservation back.

Bootstrap creates the durable StorageSession via storage-v2 (not legacy
/api/agents/ingest). Runtime binding/progress ticks stay on the runtime
batch path because they wake workspace SSE.

Runs forever. Re-uses the same canary session_id across restarts so all
probes aggregate into one session on the server.

Usage:
    LONGHOUSE_CANARY_URL=https://your-instance.longhouse.ai \
    LONGHOUSE_AGENTS_TOKEN=<agents-device-token> \
    LONGHOUSE_CANARY_TOKEN=<shared-secret-set-on-server> \
    python3 scripts/canary/producer.py

The AGENTS token authenticates storage-v2 + runtime batch.
The CANARY token authenticates /api/telemetry/canary-observation (metrics
observation). They're separate secrets because they guard separate
concerns — ingest is per-machine, canary is pipeline-internal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
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
_STORAGE_V2_INGEST_PATH = "/api/agents/storage/v2/envelopes"
_STORAGE_V2_LANE_HEADER = "X-Longhouse-Storage-Lane"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _resolve_storage_v2_wire(script_path: Path) -> Path:
    """Resolve both the flat service bundle and repository layouts."""

    resolved = script_path.resolve()
    candidates = (
        resolved.with_name("storage_v2_wire.py"),
        resolved.parents[1] / "lib" / "storage_v2_wire.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("storage-v2 wire helper is missing from the canary bundle")


_WIRE_PATH = _resolve_storage_v2_wire(Path(__file__))


def _load_storage_v2_wire():
    spec = importlib.util.spec_from_file_location("storage_v2_wire", _WIRE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load storage-v2 wire helper from {_WIRE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_storage_v2_wire = _load_storage_v2_wire()


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
            print(f"observation post {hop} seq={canary_seq} -> {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"observation post error: {exc}", file=sys.stderr)


def _agents_headers(agents_token: str) -> dict[str, str]:
    return {"X-Agents-Token": agents_token, "Content-Type": "application/json"}


def _bootstrap_storage_v2(
    client: httpx.Client,
    *,
    base_url: str,
    agents_token: str,
    session_id: str,
) -> str:
    """Create/replay the durable canary StorageSession via storage-v2.

    Fail closed: never call legacy /api/agents/ingest and never warn-and-continue.
    Returns the durable envelope_id on success.
    """

    try:
        caps_resp = client.get(
            f"{base_url}/api/agents/storage/v2/capabilities",
            headers=_agents_headers(agents_token),
            timeout=15.0,
        )
    except Exception as exc:
        print(f"FATAL: storage-v2 capabilities network error: {exc}", file=sys.stderr)
        sys.exit(3)
    if caps_resp.status_code >= 300:
        print(
            f"FATAL: storage-v2 capabilities failed {caps_resp.status_code}: {caps_resp.text[:500]}",
            file=sys.stderr,
        )
        sys.exit(3)
    try:
        capabilities = caps_resp.json()
    except json.JSONDecodeError:
        print("FATAL: storage-v2 capabilities returned non-JSON", file=sys.stderr)
        sys.exit(3)
    if not isinstance(capabilities, dict):
        print("FATAL: storage-v2 capabilities payload is invalid", file=sys.stderr)
        sys.exit(3)
    if capabilities.get("cutover") is not True:
        print("FATAL: storage-v2 cutover is not enabled on target", file=sys.stderr)
        sys.exit(3)
    if capabilities.get("protocol_version") != 2:
        print("FATAL: storage-v2 protocol_version is not 2", file=sys.stderr)
        sys.exit(3)

    tenant_id = capabilities.get("tenant_id")
    machine_id = capabilities.get("machine_id")
    ingest_path = capabilities.get("ingest_path")
    lane_header = capabilities.get("lane_header")
    if not isinstance(tenant_id, str) or not tenant_id:
        print("FATAL: storage-v2 capabilities missing tenant_id", file=sys.stderr)
        sys.exit(3)
    if not isinstance(machine_id, str) or not machine_id:
        print("FATAL: storage-v2 capabilities missing machine_id", file=sys.stderr)
        sys.exit(3)
    if ingest_path != _STORAGE_V2_INGEST_PATH:
        print("FATAL: storage-v2 capabilities returned unexpected ingest_path", file=sys.stderr)
        sys.exit(3)
    if lane_header != _STORAGE_V2_LANE_HEADER:
        print("FATAL: storage-v2 capabilities returned unexpected lane_header", file=sys.stderr)
        sys.exit(3)

    envelope = _storage_v2_wire.build_canary_bootstrap_envelope(
        tenant_id=tenant_id,
        machine_id=machine_id,
        session_id=session_id,
    )
    expected_id = envelope["expected_envelope_id"]
    try:
        resp = client.post(
            f"{base_url}{ingest_path}",
            headers={**_agents_headers(agents_token), _STORAGE_V2_LANE_HEADER: "live"},
            json=envelope,
            timeout=30.0,
        )
    except Exception as exc:
        print(f"FATAL: storage-v2 envelope network error: {exc}", file=sys.stderr)
        sys.exit(3)
    if resp.status_code >= 300:
        print(
            f"FATAL: storage-v2 envelope commit failed {resp.status_code}: {resp.text[:500]}",
            file=sys.stderr,
        )
        sys.exit(3)
    try:
        receipt = resp.json()
    except json.JSONDecodeError:
        print("FATAL: storage-v2 envelope returned non-JSON receipt", file=sys.stderr)
        sys.exit(3)
    if not isinstance(receipt, dict):
        print("FATAL: storage-v2 envelope receipt is invalid", file=sys.stderr)
        sys.exit(3)
    if (
        receipt.get("v") != 2
        or receipt.get("raw_state") != "durable"
        or receipt.get("render_state") != "ready"
        or receipt.get("media_state") != "complete"
        or receipt.get("missing_media_hashes") != []
    ):
        print("FATAL: storage-v2 envelope receipt is not browse-ready", file=sys.stderr)
        sys.exit(3)
    if receipt.get("envelope_id") != expected_id:
        print("FATAL: storage-v2 envelope receipt identity mismatch", file=sys.stderr)
        sys.exit(3)
    object_hash = receipt.get("object_hash")
    commit_seq = receipt.get("commit_seq")
    if not isinstance(object_hash, str) or _SHA256_HEX.fullmatch(object_hash) is None:
        print("FATAL: storage-v2 envelope receipt object_hash is invalid", file=sys.stderr)
        sys.exit(3)
    if (
        not isinstance(commit_seq, str)
        or not commit_seq.isascii()
        or not commit_seq.isdecimal()
        or str(int(commit_seq)) != commit_seq
        or not 0 <= int(commit_seq) < 1 << 64
    ):
        print("FATAL: storage-v2 envelope receipt commit_seq is invalid", file=sys.stderr)
        sys.exit(3)
    print(
        f"storage-v2 bootstrap ok: session_id={session_id} envelope_id={expected_id[:12]}… "
        f"commit_seq={commit_seq}"
    )
    return expected_id


def main() -> int:
    base_url = _require_env("LONGHOUSE_CANARY_URL").rstrip("/")
    agents_token = _require_env("LONGHOUSE_AGENTS_TOKEN")
    canary_token = os.environ.get("LONGHOUSE_CANARY_TOKEN", "")
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
        # Bootstrap: durable StorageSession via storage-v2, then a binding_signal
        # so LiveSession runtime liveness exists for canary-session discovery.
        _bootstrap_storage_v2(
            client,
            base_url=base_url,
            agents_token=agents_token,
            session_id=session_id,
        )

        now = datetime.now(timezone.utc)
        bootstrap = {"events": [_binding_event(session_id, machine_name, now)]}
        try:
            resp = client.post(
                f"{base_url}/api/agents/runtime/events/batch",
                headers=_agents_headers(agents_token),
                json=bootstrap,
                timeout=15.0,
            )
            if resp.status_code >= 300:
                print(f"runtime bootstrap failed {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
                return 3
        except Exception as exc:
            print(f"runtime bootstrap network error: {exc}", file=sys.stderr)
            return 3

        while not stopping:
            seq = _next_seq()
            emitted_at = datetime.now(timezone.utc)
            payload = {"events": [_runtime_event(session_id, seq, machine_name, emitted_at)]}
            send_start = time.perf_counter()
            try:
                resp = client.post(
                    f"{base_url}/api/agents/runtime/events/batch",
                    headers=_agents_headers(agents_token),
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
                    if canary_token:
                        _post_observation(
                            client,
                            base_url,
                            canary_token,
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
