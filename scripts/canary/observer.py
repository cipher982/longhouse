#!/usr/bin/env python3
"""Observe real canary runtime updates on the owner-scoped workspace stream."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import httpx

SESSION_ID_FILE = Path(
    os.environ.get(
        "LONGHOUSE_CANARY_SESSION_FILE",
        str(Path.home() / ".longhouse" / "canary-session-id"),
    )
)
UNREACHABLE_TIMEOUT_S = int(os.environ.get("LONGHOUSE_CANARY_UNREACHABLE_S", "300"))
SESSION_READY_TIMEOUT_S = 60
SSE_READ_TIMEOUT_S = 10.0


class _FatalCanaryError(RuntimeError):
    pass


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"FATAL: missing {key}", file=sys.stderr)
        raise _FatalCanaryError(f"missing {key}")
    return value


def _post_observation(
    client: httpx.Client,
    base_url: str,
    canary_token: str,
    *,
    canary_seq: int,
    latency_ms: int,
) -> None:
    if latency_ms < 0:
        raise _FatalCanaryError(
            f"negative SSE latency for canary sequence {canary_seq}"
        )
    response = client.post(
        f"{base_url}/api/telemetry/canary-observation",
        headers={"X-Canary-Token": canary_token, "Content-Type": "application/json"},
        json={
            "canary_seq": canary_seq,
            "hop": "sse",
            "surface": "observer",
            "latency_ms": latency_ms,
        },
        timeout=10.0,
    )
    if response.status_code != 200:
        raise _FatalCanaryError(
            f"canary observation returned HTTP {response.status_code}"
        )
    try:
        receipt = response.json()
    except json.JSONDecodeError as exc:
        raise _FatalCanaryError("canary observation returned invalid JSON") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("ok") is not True
        or receipt.get("hop") != "sse"
        or receipt.get("seq") != canary_seq
    ):
        raise _FatalCanaryError("canary observation returned an invalid receipt")


def _iter_sse(response: httpx.Response):
    """Yield complete server-sent event frames, ignoring comments and heartbeats."""
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


def _canary_marker(payload: str) -> tuple[int, int, int, int, int]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise _FatalCanaryError("canary SSE event returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise _FatalCanaryError("canary SSE event is not an object")
    canary_seq = data.get("canary_seq")
    emitted_at_ms = data.get("canary_emitted_at_ms")
    server_fanout_at_ms = data.get("server_fanout_at_ms")
    server_now_ms = data.get("server_now_ms")
    pubsub_seq = data.get("pubsub_seq")
    if (
        type(canary_seq) is not int
        or canary_seq < 0
        or type(emitted_at_ms) is not int
        or emitted_at_ms <= 0
        or type(server_fanout_at_ms) is not int
        or server_fanout_at_ms <= 0
        or type(server_now_ms) is not int
        or server_now_ms <= 0
        or type(pubsub_seq) is not int
        or pubsub_seq <= 0
    ):
        raise _FatalCanaryError(
            "canary SSE event is missing its producer sequence or timing coordinates"
        )
    return canary_seq, emitted_at_ms, server_fanout_at_ms, server_now_ms, pubsub_seq


def main() -> int:
    try:
        base_url = _require_env("LONGHOUSE_CANARY_URL").rstrip("/")
        canary_token = _require_env("LONGHOUSE_CANARY_TOKEN")
        agents_token = _require_env("LONGHOUSE_AGENTS_TOKEN")
    except _FatalCanaryError:
        return 2
    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    ready_deadline = time.monotonic() + SESSION_READY_TIMEOUT_S
    while not SESSION_ID_FILE.is_file() and not stopping:
        if time.monotonic() >= ready_deadline:
            print(
                f"FATAL: {SESSION_ID_FILE} was not created by the canary producer",
                file=sys.stderr,
            )
            return 3
        time.sleep(0.25)
    if stopping:
        return 0
    session_id = SESSION_ID_FILE.read_text().strip()
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError("noncanonical UUID")
    except ValueError:
        print(
            f"FATAL: {SESSION_ID_FILE} does not contain a canonical UUID",
            file=sys.stderr,
        )
        return 3

    stream_url = f"{base_url}/api/telemetry/canary-stream?session_id={session_id}"
    print(f"canary observer: session_id={session_id} stream={stream_url}")
    timeout = httpx.Timeout(
        connect=10.0, read=SSE_READ_TIMEOUT_S, write=10.0, pool=10.0
    )
    last_successful_read_at = time.monotonic()
    backoff_s = 1.0
    last_canary_seq = -1
    stream_established = False

    with httpx.Client(http2=False, timeout=timeout) as client:
        while not stopping:
            try:
                with client.stream(
                    "GET",
                    stream_url,
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "X-Canary-Token": canary_token,
                        "X-Agents-Token": agents_token,
                    },
                ) as response:
                    if response.status_code != 200:
                        message = f"canary stream returned HTTP {response.status_code}"
                        # The producer reserves its stable ID before the server
                        # commits bootstrap. Wait only during initial readiness.
                        if (
                            response.status_code == 404
                            and not stream_established
                            and time.monotonic() < ready_deadline
                        ):
                            raise RuntimeError(
                                "canary producer session is not yet visible"
                            )
                        if 400 <= response.status_code < 500:
                            raise _FatalCanaryError(message)
                        raise RuntimeError(message)
                    if (
                        not response.headers.get("content-type", "")
                        .lower()
                        .startswith("text/event-stream")
                    ):
                        raise _FatalCanaryError(
                            "canary stream returned a non-SSE content type"
                        )
                    stream_established = True
                    backoff_s = 1.0
                    for event_name, payload in _iter_sse(response):
                        if stopping:
                            break
                        last_successful_read_at = time.monotonic()
                        if event_name in {"connected", "heartbeat"}:
                            continue
                        if event_name == "error":
                            raise _FatalCanaryError(
                                f"canary stream error: {payload[:200]}"
                            )
                        if event_name != "canary_observation":
                            raise _FatalCanaryError(
                                f"unexpected canary stream event: {event_name!r}"
                            )
                        (
                            canary_seq,
                            emitted_at_ms,
                            _fanout_ms,
                            _server_now_ms,
                            _pubsub_seq,
                        ) = _canary_marker(payload)
                        if canary_seq <= last_canary_seq:
                            raise _FatalCanaryError(
                                f"canary sequence did not advance: {canary_seq}"
                            )
                        latency_ms = int(time.time() * 1000) - emitted_at_ms
                        if latency_ms < 0 or latency_ms > 600_000:
                            raise _FatalCanaryError(
                                f"invalid SSE latency for canary sequence {canary_seq}: {latency_ms}ms"
                            )
                        try:
                            _post_observation(
                                client,
                                base_url,
                                canary_token,
                                canary_seq=canary_seq,
                                latency_ms=latency_ms,
                            )
                        except Exception as exc:
                            raise _FatalCanaryError(
                                f"could not record SSE observation: {exc}"
                            ) from exc
                        last_canary_seq = canary_seq
                if stopping:
                    break
                raise RuntimeError("canary SSE stream closed")
            except _FatalCanaryError as exc:
                print(f"FATAL: {exc}", file=sys.stderr)
                return 3
            except Exception as exc:
                print(f"SSE error ({exc.__class__.__name__}): {exc}", file=sys.stderr)
                if time.monotonic() - last_successful_read_at > UNREACHABLE_TIMEOUT_S:
                    print(
                        f"SSE unreachable > {UNREACHABLE_TIMEOUT_S}s; exiting for supervisor restart",
                        file=sys.stderr,
                    )
                    return 3
                slept = 0.0
                while slept < backoff_s and not stopping:
                    duration = min(0.5, backoff_s - slept)
                    time.sleep(duration)
                    slept += duration
                backoff_s = min(30.0, backoff_s * 2)

    print("canary observer stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
