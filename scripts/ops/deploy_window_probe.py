#!/usr/bin/env python3
"""Measure HTTP, control WebSocket, and SSE behavior during a Runtime Host restart."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_INVALID_JSON = object()
DEVICE_ID = "deploy-window-probe"
MACHINE_NAME = DEVICE_ID
HEALTH_INTERVAL_S = 0.25
WRITE_INTERVAL_S = 1.0
RECONNECT_INTERVAL_S = 0.5
CONTROL_CONNECT_TIMEOUT_S = 3.0
SSE_CONNECT_TIMEOUT_S = 3.0


def _wall_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonlRecorder:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8")
        self._lock = asyncio.Lock()

    async def emit(self, channel: str, **fields: Any) -> None:
        observation = {
            "wall_time": _wall_time(),
            "monotonic": round(time.monotonic(), 6),
            "channel": channel,
            **fields,
        }
        async with self._lock:
            self._file.write(json.dumps(observation, separators=(",", ":"), ensure_ascii=False) + "\n")
            self._file.flush()

    def close(self) -> None:
        self._file.close()


def _http_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
    if scheme is None:
        raise ValueError("--base-url must use http:// or https://")
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/") + "/api/agents/control/ws", "", ""))


def _transport_error_class(error: BaseException) -> str:
    name = type(error).__name__.lower()
    if "readtimeout" in name:
        return "read_timeout"
    if "connect" in name:
        return "connect"
    return "other"


def _decode_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _INVALID_JSON


def _is_html(content_type: str | None, body: str) -> bool:
    if "text/html" in (content_type or "").lower():
        return True
    lowered = body.lstrip().lower()
    return lowered.startswith(("<!doctype html", "<html", "<!--"))


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    if seconds <= 0:
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _periodic(
    stop: asyncio.Event,
    deadline: float,
    interval: float,
    operation: Any,
) -> None:
    next_at = time.monotonic()
    while not stop.is_set() and time.monotonic() < deadline:
        await operation()
        next_at += interval
        now = time.monotonic()
        if next_at < now:
            next_at = now
        if await _wait_or_stop(stop, min(next_at - now, max(0.0, deadline - now))):
            return


async def _health_channel(client: Any, recorder: JsonlRecorder, base_url: str, stop: asyncio.Event, deadline: float) -> None:
    async def poll() -> None:
        started = time.monotonic()
        try:
            response = await client.get(_http_url(base_url, "/api/health"), timeout=2.0)
            body = _decode_json(response.text)
            runtime = body.get("runtime") if isinstance(body, dict) else None
            build = body.get("build") if isinstance(body, dict) else None
            await recorder.emit(
                "health",
                event="response",
                status=response.status_code,
                latency_s=round(time.monotonic() - started, 6),
                runtime=runtime if isinstance(runtime, dict) else None,
                build_commit=build.get("commit") if isinstance(build, dict) else None,
            )
        except Exception as exc:
            await recorder.emit(
                "health",
                event="response",
                status=None,
                latency_s=round(time.monotonic() - started, 6),
                runtime=None,
                build_commit=None,
                transport_error_class=_transport_error_class(exc),
                error=type(exc).__name__,
            )

    await _periodic(stop, deadline, HEALTH_INTERVAL_S, poll)


async def _write_channel(
    client: Any,
    recorder: JsonlRecorder,
    base_url: str,
    token: str,
    device_id: str,
    stop: asyncio.Event,
    deadline: float,
) -> None:
    async def post() -> None:
        started = time.monotonic()
        try:
            response = await client.post(
                _http_url(base_url, "/api/agents/heartbeat"),
                json={},
                headers={"X-Agents-Token": token, "X-Longhouse-Machine-Id": device_id},
                timeout=6.0,
            )
            body_text = response.text
            decoded = _decode_json(body_text)
            is_json = decoded is not _INVALID_JSON
            error_payload = decoded
            if isinstance(decoded, dict) and isinstance(decoded.get("detail"), dict):
                if "code" in decoded["detail"]:
                    error_payload = decoded["detail"]
            content_type = response.headers.get("content-type")
            await recorder.emit(
                "write",
                event="response",
                status=response.status_code,
                latency_s=round(time.monotonic() - started, 6),
                content_type=content_type,
                is_json=is_json,
                code=error_payload.get("code") if isinstance(error_payload, dict) else None,
                retryable=error_payload.get("retryable") if isinstance(error_payload, dict) else None,
                retry_after=response.headers.get("retry-after"),
                is_html=_is_html(content_type, body_text),
                transport_error_class=None,
            )
        except Exception as exc:
            await recorder.emit(
                "write",
                event="response",
                status=None,
                latency_s=round(time.monotonic() - started, 6),
                content_type=None,
                is_json=False,
                code=None,
                retryable=None,
                retry_after=None,
                is_html=False,
                transport_error_class=_transport_error_class(exc),
                error=type(exc).__name__,
            )

    await _periodic(stop, deadline, WRITE_INTERVAL_S, post)


async def _sse_messages(lines: Any):
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if event_name is not None or data_lines:
                yield event_name or "message", "\n".join(data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if event_name is not None or data_lines:
        yield event_name or "message", "\n".join(data_lines)


async def _sse_channel(
    client: Any,
    recorder: JsonlRecorder,
    base_url: str,
    token: str,
    device_id: str,
    stop: asyncio.Event,
    deadline: float,
) -> None:

    url = _http_url(base_url, "/api/agents/sessions/stream")
    headers = {"X-Agents-Token": token, "Accept": "text/event-stream"}
    params = {"device_id": device_id, "skip_initial_replay": "true"}
    attempt = 0
    while not stop.is_set() and time.monotonic() < deadline:
        attempt += 1
        started = time.monotonic()
        established = False
        disconnected = False
        response = None
        try:
            request = client.build_request("GET", url, headers=headers, params=params, timeout=None)
            response = await asyncio.wait_for(
                client.send(request, stream=True),
                timeout=SSE_CONNECT_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await recorder.emit(
                "sse",
                event="connect_attempt",
                attempt=attempt,
                outcome="error",
                latency_s=round(time.monotonic() - started, 6),
                error_class=type(exc).__name__,
                error=str(exc)[:240],
            )
        else:
            try:
                latency = round(time.monotonic() - started, 6)
                if response.status_code < 200 or response.status_code >= 300:
                    await recorder.emit(
                        "sse",
                        event="connect_attempt",
                        attempt=attempt,
                        outcome="http_error",
                        status=response.status_code,
                        latency_s=latency,
                    )
                else:
                    established = True
                    await recorder.emit(
                        "sse",
                        event="connect_attempt",
                        attempt=attempt,
                        outcome="connected",
                        status=response.status_code,
                        latency_s=latency,
                    )
                    messages = _sse_messages(response.aiter_lines())
                    while not stop.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            name, data = await asyncio.wait_for(messages.__anext__(), timeout=remaining)
                        except StopAsyncIteration:
                            disconnected = True
                            await recorder.emit("sse", event="disconnect", reason="stream_ended")
                            break
                        except asyncio.TimeoutError:
                            break
                        if name in {"connected", "host_lifecycle"}:
                            decoded_data = _decode_json(data)
                            await recorder.emit(
                                "sse",
                                event="message",
                                name=name,
                                data=decoded_data if decoded_data is not _INVALID_JSON else data,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if established:
                    disconnected = True
                    await recorder.emit(
                        "sse",
                        event="disconnect",
                        reason="transport_error",
                        transport_error_class=_transport_error_class(exc),
                        error=type(exc).__name__,
                    )
                else:
                    await recorder.emit(
                        "sse",
                        event="connect_attempt",
                        attempt=attempt,
                        outcome="error",
                        latency_s=round(time.monotonic() - started, 6),
                        error_class=type(exc).__name__,
                        error=str(exc)[:240],
                    )
            finally:
                await response.aclose()
        if not disconnected and established:
            return
        if await _wait_or_stop(stop, min(RECONNECT_INTERVAL_S, max(0.0, deadline - time.monotonic()))):
            return


async def _control_channel(
    recorder: JsonlRecorder,
    base_url: str,
    token: str,
    device_id: str,
    stop: asyncio.Event,
    deadline: float,
) -> None:
    import websockets

    url = _websocket_url(base_url)
    headers = {"X-Agents-Token": token}
    hello = {
        "type": "hello",
        "schema_version": 1,
        "device_id": device_id,
        "machine_name": MACHINE_NAME,
        "engine_build": "deploy-window-probe",
        "supports": [],
        "provider_readiness": {},
    }
    async def emit_frame(frame: str | bytes) -> None:
        payload = _decode_json(frame) if isinstance(frame, str) else None
        frame_kind = "text" if isinstance(frame, str) else "binary"
        frame_type = payload.get("type") if isinstance(payload, dict) else None
        fields: dict[str, Any] = {"event": "frame", "type": frame_type, "frame_kind": frame_kind}
        if frame_type == "host.lifecycle" and isinstance(payload, dict):
            fields["host_lifecycle"] = payload
        await recorder.emit("control", **fields)
    attempt = 0
    while not stop.is_set() and time.monotonic() < deadline:
        attempt += 1
        started = time.monotonic()
        try:
            connection = await websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=min(CONTROL_CONNECT_TIMEOUT_S, max(0.001, deadline - started)),
                close_timeout=1.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await recorder.emit(
                "control",
                event="connect_attempt",
                attempt=attempt,
                outcome="error",
                latency_s=round(time.monotonic() - started, 6),
                error_class=type(exc).__name__,
                error=str(exc)[:240],
            )
            if await _wait_or_stop(stop, min(RECONNECT_INTERVAL_S, max(0.0, deadline - time.monotonic()))):
                return
            continue

        try:
            await connection.send(json.dumps(hello, separators=(",", ":")))
            await connection.send('{"type":"heartbeat"}')
            await recorder.emit("control", event="heartbeat_sent", type="heartbeat")
            first_frame = await asyncio.wait_for(
                connection.recv(),
                timeout=min(CONTROL_CONNECT_TIMEOUT_S, max(0.001, deadline - time.monotonic())),
            )
        except asyncio.CancelledError:
            await connection.close()
            raise
        except Exception as exc:
            await recorder.emit(
                "control",
                event="connect_attempt",
                attempt=attempt,
                outcome="error",
                latency_s=round(time.monotonic() - started, 6),
                close_code=getattr(connection, "close_code", None),
                close_reason=getattr(connection, "close_reason", None),
                error_class=type(exc).__name__,
                error=str(exc)[:240],
            )
            await connection.close()
            if await _wait_or_stop(stop, min(RECONNECT_INTERVAL_S, max(0.0, deadline - time.monotonic()))):
                return
            continue

        await emit_frame(first_frame)
        await recorder.emit(
            "control",
            event="connect_attempt",
            attempt=attempt,
            outcome="connected",
            latency_s=round(time.monotonic() - started, 6),
        )
        disconnected = False
        try:
            next_heartbeat = time.monotonic() + 10.0
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                now = time.monotonic()
                if now >= next_heartbeat:
                    await connection.send('{"type":"heartbeat"}')
                    await recorder.emit("control", event="heartbeat_sent", type="heartbeat")
                    next_heartbeat = time.monotonic() + 10.0
                    continue
                try:
                    frame = await asyncio.wait_for(connection.recv(), timeout=min(remaining, next_heartbeat - now))
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        break
                    if time.monotonic() >= next_heartbeat:
                        await connection.send('{"type":"heartbeat"}')
                        await recorder.emit("control", event="heartbeat_sent", type="heartbeat")
                        next_heartbeat = time.monotonic() + 10.0
                    continue
                await emit_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            disconnected = True
            close_code = getattr(connection, "close_code", None)
            close_reason = getattr(connection, "close_reason", None)
            await recorder.emit(
                "control",
                event="disconnect",
                close_code=close_code,
                close_reason=close_reason,
                error_class=type(exc).__name__ if close_code is None else None,
                error=str(exc)[:240] if close_code is None else None,
            )
        finally:
            await connection.close()

        if not disconnected or stop.is_set() or time.monotonic() >= deadline:
            return
        if await _wait_or_stop(stop, min(RECONNECT_INTERVAL_S, max(0.0, deadline - time.monotonic()))):
            return


async def _record(args: argparse.Namespace) -> None:
    try:
        import httpx
    except ImportError as exc:
        raise SystemExit("record requires httpx; run with `uv run --with httpx --with websockets`") from exc
    try:
        import websockets  # noqa: F401
    except ImportError as exc:
        raise SystemExit("record requires websockets; run with `uv run --with httpx --with websockets`") from exc

    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(f"environment variable {args.token_env!r} is unset or empty")
    if not args.device_id.strip():
        raise SystemExit("--device-id must be non-empty")
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--base-url must be an absolute http:// or https:// URL")

    recorder = JsonlRecorder(Path(args.out))
    stop = asyncio.Event()
    deadline = time.monotonic() + args.duration
    try:
        async with httpx.AsyncClient() as client:
            tasks = [
                asyncio.create_task(_health_channel(client, recorder, args.base_url, stop, deadline)),
                asyncio.create_task(_write_channel(client, recorder, args.base_url, token, args.device_id, stop, deadline)),
                asyncio.create_task(_control_channel(recorder, args.base_url, token, args.device_id, stop, deadline)),
                asyncio.create_task(_sse_channel(client, recorder, args.base_url, token, args.device_id, stop, deadline)),
            ]
            try:
                await asyncio.sleep(args.duration)
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        recorder.close()


def _mono(record: dict[str, Any]) -> float | None:
    value = record.get("monotonic")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _runtime(record: dict[str, Any]) -> dict[str, Any] | None:
    value = record.get("runtime")
    if isinstance(value, dict):
        return value
    if record.get("runtime_epoch") is not None or record.get("runtime_admission") is not None:
        return {"epoch": record.get("runtime_epoch"), "admission": record.get("runtime_admission")}
    return None


def _lifecycle_state(data: Any) -> str | None:
    if isinstance(data, str):
        data = _decode_json(data)
    if not isinstance(data, dict):
        return None
    nested = data.get("host_lifecycle")
    if isinstance(nested, dict):
        data = nested
    state = data.get("state")
    return state if isinstance(state, str) else None


def _connected_admission_open(data: Any) -> bool:
    if isinstance(data, str):
        data = _decode_json(data)
    if not isinstance(data, dict):
        return False
    if data.get("admission") == "open":
        return True
    runtime = data.get("runtime")
    return isinstance(runtime, dict) and runtime.get("admission") == "open"

def _channel_summary(
    records: list[dict[str, Any]],
    channel: str,
    serving_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    observations = [row for row in records if row.get("channel") == channel]
    disconnect = next((row for row in observations if row.get("event") == "disconnect"), None)
    disconnect_at = _mono(disconnect) if disconnect else None
    reconnect = next(
        (
            row
            for row in observations
            if row.get("event") == "connect_attempt"
            and row.get("outcome") == "connected"
            and disconnect_at is not None
            and (_mono(row) or 0.0) > disconnect_at
        ),
        None,
    )
    reconnect_at = _mono(reconnect) if reconnect else None
    evidence_at = serving_evidence.get("at_monotonic") if serving_evidence else None
    after_open = reconnect_at - evidence_at if reconnect_at is not None and evidence_at is not None and reconnect_at >= evidence_at else None
    after_disconnect = reconnect_at - disconnect_at if reconnect_at is not None and disconnect_at is not None else None
    states: list[str] = []
    if channel == "control":
        for row in observations:
            if row.get("event") != "frame" or row.get("type") != "host.lifecycle":
                continue
            state = _lifecycle_state(row.get("host_lifecycle"))
            if state is not None:
                states.append(state)
    else:
        for row in observations:
            if row.get("event") != "message" or row.get("name") != "host_lifecycle":
                continue
            state = _lifecycle_state(row.get("data"))
            if state is not None:
                states.append(state)
    return {
        "disconnect_monotonic": disconnect_at,
        "close_code": disconnect.get("close_code") if disconnect else None,
        "close_reason": disconnect.get("close_reason") if disconnect else None,
        "reconnect_monotonic": reconnect_at,
        "reconnect_after_disconnect_s": round(after_disconnect, 6) if after_disconnect is not None else None,
        "reconnect_after_open_s": round(after_open, 6) if after_open is not None else None,
        "reconnect_reference": serving_evidence.get("source") if serving_evidence else None,
        "host_lifecycle_states": states,
    }


def analyze_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(enumerate(records), key=lambda item: (_mono(item[1]) is None, _mono(item[1]) or 0.0, item[0]))
    rows = [row for _, row in ordered]
    health = [row for row in rows if row.get("channel") == "health" and row.get("event") == "response"]
    epoch_change: dict[str, Any] | None = None
    previous_epoch: Any = None
    for row in health:
        runtime = _runtime(row)
        epoch = runtime.get("epoch") if runtime else None
        if epoch is None:
            continue
        if previous_epoch is not None and epoch != previous_epoch:
            epoch_change = {
                "at_monotonic": _mono(row),
                "wall_time": row.get("wall_time"),
                "from_epoch": previous_epoch,
                "to_epoch": epoch,
            }
            break
        previous_epoch = epoch

    open_admission: dict[str, Any] | None = None
    change_at = epoch_change.get("at_monotonic") if epoch_change else None
    if change_at is not None:
        for row in health:
            at = _mono(row)
            runtime = _runtime(row)
            if at is not None and at >= change_at and runtime and runtime.get("admission") == "open":
                open_admission = {
                    "at_monotonic": at,
                    "wall_time": row.get("wall_time"),
                    "after_epoch_change_s": round(at - change_at, 6),
                }
                break

    writes = [row for row in rows if row.get("channel") == "write" and row.get("event", "response") == "response"]
    failed_counts = {"typed": 0, "untyped_json": 0, "html": 0, "transport": 0, "timeout": 0, "other": 0}
    first_failure: dict[str, Any] | None = None
    first_success_after: dict[str, Any] | None = None
    no_untyped_5xx = True
    for row in writes:
        status = row.get("status")
        try:
            status_code = int(status) if status is not None else None
        except (TypeError, ValueError):
            status_code = None
        error_class = str(row.get("transport_error_class") or "").lower()
        failed = status_code is None or not 200 <= status_code < 300
        if not failed:
            if first_failure is not None and first_success_after is None:
                failure_at = _mono(first_failure)
                success_at = _mono(row)
                if failure_at is not None and success_at is not None and success_at >= failure_at:
                    first_success_after = row
            continue
        if first_failure is None:
            first_failure = row
        if status_code is not None and 500 <= status_code < 600:
            if not (row.get("is_json") and row.get("code")):
                no_untyped_5xx = False
        if error_class:
            if "timeout" in error_class:
                failed_counts["timeout"] += 1
            else:
                failed_counts["transport"] += 1
        elif row.get("is_json"):
            failed_counts["typed" if row.get("code") else "untyped_json"] += 1
        elif row.get("is_html"):
            failed_counts["html"] += 1
        else:
            failed_counts["other"] += 1

    window_start = _mono(first_failure) if first_failure else None
    window_end = _mono(first_success_after) if first_success_after else None
    closed_writes_s = round(window_end - window_start, 6) if window_start is not None and window_end is not None else None
    disconnect_times = [
        at
        for row in rows
        if row.get("channel") in {"control", "sse"} and row.get("event") == "disconnect"
        for at in [_mono(row)]
        if at is not None
    ]
    restart_times = [at for at in [window_start, *disconnect_times] if at is not None]
    restart_start = min(restart_times) if restart_times else None
    evidence_floor = restart_start if restart_start is not None else change_at
    evidence_candidates: list[tuple[float, str, dict[str, Any]]] = []

    def offer_evidence(source: str, row: dict[str, Any]) -> None:
        at = _mono(row)
        if at is not None and evidence_floor is not None and at >= evidence_floor:
            evidence_candidates.append((at, source, row))

    for row in health:
        runtime = _runtime(row)
        if runtime and runtime.get("admission") == "open":
            offer_evidence("health_admission_open", row)
    for row in rows:
        if row.get("channel") == "control" and row.get("event") == "frame" and row.get("type") == "host.lifecycle":
            if _lifecycle_state(row.get("host_lifecycle")) == "serving":
                offer_evidence("control_host_lifecycle", row)
        elif row.get("channel") == "sse" and row.get("event") == "message":
            if row.get("name") == "host_lifecycle" and _lifecycle_state(row.get("data")) == "serving":
                offer_evidence("sse_host_lifecycle", row)
            elif row.get("name") == "connected" and _connected_admission_open(row.get("data")):
                offer_evidence("sse_connected_admission_open", row)
    for row in writes:
        try:
            status_code = int(row.get("status")) if row.get("status") is not None else None
        except (TypeError, ValueError):
            status_code = None
        if status_code is not None and 200 <= status_code < 300:
            offer_evidence("accepted_write", row)
    serving_evidence = None
    if evidence_candidates:
        evidence_at, evidence_source, evidence_row = min(evidence_candidates, key=lambda candidate: candidate[0])
        serving_evidence = {
            "source": evidence_source,
            "at_monotonic": evidence_at,
            "wall_time": evidence_row.get("wall_time"),
        }
        if restart_start is not None:
            serving_evidence["after_restart_start_s"] = round(evidence_at - restart_start, 6)
    writes_summary = {
        "first_failure_monotonic": window_start,
        "first_failure_wall_time": first_failure.get("wall_time") if first_failure else None,
        "first_success_after_failure_monotonic": window_end,
        "first_success_after_failure_wall_time": first_success_after.get("wall_time") if first_success_after else None,
        "closed_writes_s": closed_writes_s,
        "failed_by_class": failed_counts,
    }
    health_summary = {
        "epoch_change": epoch_change,
        "first_open_after_change": open_admission,
        "first_serving_evidence": serving_evidence,
    }
    control_summary = _channel_summary(rows, "control", serving_evidence)
    sse_summary = _channel_summary(rows, "sse", serving_evidence)
    return {
        "observation_count": len(rows),
        "writes": writes_summary,
        "health": health_summary,
        "control": control_summary,
        "sse": sse_summary,
        "verdict": {
            "no_untyped_5xx": no_untyped_5xx,
            "closed_writes_s": closed_writes_s,
            "control_reconnect_after_open_within_1s": (
                control_summary["reconnect_after_open_s"] is not None
                and control_summary["reconnect_after_open_s"] <= 1.0
            ),
        },
    }


def analyze(path: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(record)
    return analyze_records(records)


def _positive_duration(value: str) -> float:
    try:
        duration = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be a number greater than zero") from exc
    if duration <= 0:
        raise argparse.ArgumentTypeError("duration must be a number greater than zero")
    return duration


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure Runtime Host restart behavior and analyze JSONL observations.")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="record health, writes, control WebSocket, and SSE concurrently")
    record.add_argument("--base-url", required=True, help="Runtime Host base URL, e.g. https://longhouse.example")
    record.add_argument("--token-env", required=True, metavar="VAR", help="environment variable containing the device token")
    record.add_argument("--device-id", default=DEVICE_ID, help="token-bound device id; default is deploy-window-probe")
    record.add_argument("--duration", required=True, type=_positive_duration, metavar="SECONDS")
    record.add_argument("--out", required=True, metavar="PATH.jsonl", help="write observations to this JSONL file")
    analyze_parser = commands.add_parser("analyze", help="summarize a recorded JSONL probe")
    analyze_parser.add_argument("path", metavar="PATH.jsonl")
    analyze_parser.add_argument("--json", action="store_true", help="print the complete summary as JSON")
    return parser


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"Observations: {summary['observation_count']}")
    print(f"Writes closed: {summary['writes']['closed_writes_s']} s")
    print(f"Failed writes: {json.dumps(summary['writes']['failed_by_class'], sort_keys=True)}")
    print(f"Epoch change: {summary['health']['epoch_change']}")
    print(f"First open admission: {summary['health']['first_open_after_change']}")
    print(f"Control: {json.dumps(summary['control'], sort_keys=True)}")
    print(f"SSE: {json.dumps(summary['sse'], sort_keys=True)}")
    print(f"Verdict: {json.dumps(summary['verdict'], sort_keys=True)}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "record":
        try:
            asyncio.run(_record(args))
        except KeyboardInterrupt:
            return 130
        return 0
    try:
        summary = analyze(Path(args.path))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
