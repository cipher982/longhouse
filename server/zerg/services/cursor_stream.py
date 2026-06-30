"""Cursor managed-stream decoder (``cursor-agent --print stream-json``).

Parses the NDJSON stream emitted by ``cursor-agent --print --output-format
stream-json`` into Longhouse ``EventIngest`` rows with **real per-event
timestamps** (``timestamp_ms`` on assistant/tool_call events, plus
``startedAtMs``/``completedAtMs`` on tool calls). This is the managed
observability surface for Cursor and the only place Cursor exposes true
per-event absolute timing; see ``docs/specs/cursor-transcript-format.md``.

Scope (v1): the documented stream-json event shapes — ``system`` (init),
``user``, ``assistant``, ``tool_call`` (started/completed), ``result``.
Unknown event types are tolerated and counted. The module is parse-only and
side-effect free; callers (the CLI wrapper) feed the resulting
``SessionIngest`` through ``/api/agents/ingest``.

Timestamp strategy
-------------------
- assistant / tool_call: real ``timestamp_ms`` (or ``startedAtMs`` /
  ``completedAtMs`` on the tool payload when present).
- user / system / result: no provider timestamp. Backfill with the last
  known real timestamp (monotonic carry-forward). The session ``started_at``
  is the first real timestamp if any, else wall-clock now.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from zerg.services.agents.models import EventIngest
    from zerg.services.agents.models import SessionIngest

PROVIDER = "cursor"

# Sentinel for leading unstamped events (system/user arrive before the first
# real timestamp_ms). Repaired to the first real timestamp at build() time so
# we never let wall-clock now() pollute event ordering when real timestamps
# happen to precede the current wall-clock.
_PENDING_TS = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class CursorStreamDiagnostics:
    session_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    event_count: int = 0
    unknown_event_types: dict[str, int] = field(default_factory=dict)
    timestamp_fidelity: str = "real"  # stream carries real per-event ts
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    # tool_call events seen without a usable toolCallId (linkage gap)
    unlinked_tool_calls: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ms_to_datetime(ms: int | float | str | None) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_name_from_payload(tool_call: dict[str, Any]) -> str | None:
    """Derive a tool name from the ``tool_call`` payload.

    The payload is heterogeneous: a single key ending in ``ToolCall`` (or
    ``ToolCallResult``) names the tool kind, e.g. ``shellToolCall``. Strip the
    suffix to get ``shell``. Fall back to any non-meta key.
    """
    meta_keys = {"toolCallId", "startedAtMs", "completedAtMs", "hookAdditionalContexts"}
    for key in tool_call:
        if key in meta_keys:
            continue
        if key.endswith("ToolCall"):
            return key[: -len("ToolCall")]
        if key.endswith("ToolCallResult"):
            return key[: -len("ToolCallResult")]
    for key in tool_call:
        if key not in meta_keys:
            return key
    return None


def _tool_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Extract the args dict from a tool_call payload, dropping linkage keys."""
    meta_keys = {"toolCallId", "conversationId"}
    for key in tool_call:
        if key.endswith("ToolCall") or key.endswith("ToolCallResult"):
            args = tool_call[key].get("args")
            if isinstance(args, dict):
                return {k: v for k, v in args.items() if k not in meta_keys}
            return {"value": args} if args is not None else {}
    return {}


def _tool_result_text(tool_call: dict[str, Any]) -> str:
    """Extract human-readable tool result text from a completed tool_call."""
    for key in tool_call:
        if key.endswith("ToolCall") or key.endswith("ToolCallResult"):
            result = tool_call[key].get("result")
            if result is None:
                return ""
            success = result.get("success") if isinstance(result, dict) else None
            if isinstance(success, dict):
                # Shell-style results carry stdout/stderr/exitCode; generalize.
                parts: list[str] = []
                stdout = success.get("stdout")
                stderr = success.get("stderr")
                if stdout:
                    parts.append(str(stdout))
                if stderr:
                    parts.append(str(stderr))
                if success.get("exitCode") not in (None, 0):
                    parts.append(f"[exit {success.get('exitCode')}]")
                if parts:
                    return "\n".join(parts)
                # Non-shell success: coerce the whole success body.
                return _coerce_text(success)
            return _coerce_text(result)
    return ""


def _clean_tool_call_id(raw: str | None) -> str | None:
    """Cursor call_ids are sometimes ``call_...\nfc_...``; take the first part."""
    if not raw:
        return None
    return raw.split("\n", 1)[0].strip() or None


# ---------------------------------------------------------------------------
# Stream builder
# ---------------------------------------------------------------------------


class CursorStreamBuilder:
    """Accumulate stream-json events into a ``SessionIngest``.

    Feed each parsed stream line via :meth:`feed`. Call :meth:`build` once the
    stream ends. Timestamps are carried forward monotonically for events that
    lack a provider timestamp (user/system/result).
    """

    def __init__(
        self,
        *,
        environment: str = "production",
        device_id: str | None = None,
        device_name: str | None = None,
    ) -> None:
        from zerg.services.agents.models import EventIngest  # deferred: avoid DB settings import

        self._EventIngest = EventIngest
        self.environment = environment
        self.device_id = device_id
        self.device_name = device_name
        self.events: list[EventIngest] = []
        self.diag = CursorStreamDiagnostics()
        self._last_real_ts: datetime | None = None
        self._first_real_ts: datetime | None = None
        self._source_path = "cursor-agent:stream-json"

    # -- timestamp resolution --

    def _resolve_ts(self, ms: int | float | str | None) -> datetime:
        ts = _ms_to_datetime(ms)
        if ts is not None:
            self._last_real_ts = ts
            if self._first_real_ts is None:
                self._first_real_ts = ts
            return ts
        # Carry forward the last real timestamp for unstamped events (result,
        # mid-stream user). For leading unstamped events with no prior real ts
        # (system/user before the first assistant), defer via a sentinel that
        # build() repairs to the first real timestamp — never wall-clock now,
        # which can precede the stream's real timestamps and break ordering.
        if self._last_real_ts is not None:
            return self._last_real_ts
        return _PENDING_TS

    # -- event emission --

    def _emit(
        self,
        role: str,
        *,
        content_text: str | None = None,
        tool_name: str | None = None,
        tool_input_json: dict[str, Any] | None = None,
        tool_output_text: str | None = None,
        tool_call_id: str | None = None,
        timestamp: datetime,
        raw_json: str,
    ) -> None:
        self.events.append(
            self._EventIngest(
                role=role,
                content_text=content_text,
                tool_name=tool_name,
                tool_input_json=tool_input_json,
                tool_output_text=tool_output_text,
                tool_call_id=tool_call_id,
                timestamp=timestamp,
                source_path=self._source_path,
                raw_json=raw_json,
            )
        )

    # -- per-type handling --

    def _on_system(self, o: dict[str, Any], raw: str) -> None:
        if o.get("subtype") == "init" or not self.diag.session_id:
            self.diag.session_id = o.get("session_id") or self.diag.session_id
            self.diag.cwd = o.get("cwd") or self.diag.cwd
            self.diag.model = o.get("model") or self.diag.model
        ts = self._resolve_ts(o.get("timestamp_ms"))
        # Emit a lightweight system event so the transcript carries the init
        # context (cwd/model) without fabricating user content.
        self._emit("system", content_text="", timestamp=ts, raw_json=raw)

    def _on_user(self, o: dict[str, Any], raw: str) -> None:
        msg = o.get("message") or {}
        content = msg.get("content") or []
        ts = self._resolve_ts(o.get("timestamp_ms"))
        if isinstance(content, str):
            self._emit("user", content_text=content, timestamp=ts, raw_json=raw)
            return
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                self._emit("user", content_text=_coerce_text(block.get("text")), timestamp=ts, raw_json=raw)

    def _on_assistant(self, o: dict[str, Any], raw: str) -> None:
        msg = o.get("message") or {}
        content = msg.get("content") or []
        ts = self._resolve_ts(o.get("timestamp_ms"))
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                self._emit(
                    "assistant",
                    content_text=_coerce_text(block.get("text")),
                    timestamp=ts,
                    raw_json=raw,
                )
            elif btype == "reasoning":
                self._emit(
                    "assistant",
                    content_text=_coerce_text(block.get("text")),
                    timestamp=ts,
                    raw_json=raw,
                )
            elif btype == "tool_use":
                self._emit(
                    "assistant",
                    tool_name=block.get("name"),
                    tool_input_json=block.get("input") if isinstance(block.get("input"), dict) else {},
                    tool_call_id=_clean_tool_call_id(block.get("id")),
                    timestamp=ts,
                    raw_json=raw,
                )

    def _on_tool_call(self, o: dict[str, Any], raw: str) -> None:
        subtype = o.get("subtype")
        tool_call = o.get("tool_call") or {}
        tool_name = _tool_name_from_payload(tool_call)
        tool_call_id = _clean_tool_call_id(tool_call.get("toolCallId") or o.get("call_id"))
        if not tool_call_id:
            self.diag.unlinked_tool_calls += 1
        # Prefer the per-tool startedAtMs/completedAtMs; fall back to timestamp_ms.
        if subtype == "completed":
            ts = self._resolve_ts(tool_call.get("completedAtMs") or o.get("timestamp_ms"))
            self._emit(
                "tool",
                tool_name=tool_name,
                tool_output_text=_tool_result_text(tool_call),
                tool_call_id=tool_call_id,
                timestamp=ts,
                raw_json=raw,
            )
        else:
            # "started" (or unknown subtype) -> the call itself.
            ts = self._resolve_ts(tool_call.get("startedAtMs") or o.get("timestamp_ms"))
            self._emit(
                "assistant",
                tool_name=tool_name,
                tool_input_json=_tool_args(tool_call),
                tool_call_id=tool_call_id,
                timestamp=ts,
                raw_json=raw,
            )

    def _on_result(self, o: dict[str, Any], raw: str) -> None:
        # Result is a turn-end marker; no per-event timestamp. Carry forward.
        self._resolve_ts(o.get("timestamp_ms"))
        # Extend ended_at by duration if present and no further real ts arrives.
        duration = o.get("duration_ms")
        if isinstance(duration, (int, float)) and duration > 0 and self._last_real_ts is not None:
            from datetime import timedelta

            ended = self._last_real_ts + timedelta(milliseconds=duration)
            self._last_real_ts = ended
            self.diag.ended_at_ms = int(ended.timestamp() * 1000)

    # -- public API --

    def feed(self, o: dict[str, Any], raw: str) -> None:
        etype = o.get("type")
        if etype == "system":
            self._on_system(o, raw)
        elif etype == "user":
            self._on_user(o, raw)
        elif etype == "assistant":
            self._on_assistant(o, raw)
        elif etype == "tool_call":
            self._on_tool_call(o, raw)
        elif etype == "result":
            self._on_result(o, raw)
        else:
            self.diag.unknown_event_types[str(etype)] = self.diag.unknown_event_types.get(str(etype), 0) + 1
        self.diag.event_count = len(self.events)

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            o = json.loads(line)
        except ValueError:
            return
        if isinstance(o, dict):
            self.feed(o, line)

    def build(self) -> "SessionIngest":
        from zerg.services.agents.models import SessionIngest

        started = self._first_real_ts or datetime.now(tz=timezone.utc)
        # Repair leading unstamped events (sentinel) to the first real ts.
        for ev in self.events:
            if ev.timestamp == _PENDING_TS:
                ev.timestamp = started
        ended = self._last_real_ts
        if ended is None or ended < started:
            ended = started
        self.diag.started_at_ms = int(started.timestamp() * 1000)
        if self.diag.ended_at_ms is None:
            self.diag.ended_at_ms = int(ended.timestamp() * 1000)

        cwd = self.diag.cwd
        session = SessionIngest(
            provider=PROVIDER,
            environment=self.environment,
            project=cwd.split("/")[-1] if cwd else None,
            cwd=cwd,
            started_at=started,
            ended_at=ended,
            provider_session_id=self.diag.session_id,
            device_id=self.device_id,
            device_name=self.device_name,
            execution_home="managed_local",
            events=self.events,
        )
        return session


# ---------------------------------------------------------------------------
# Convenience: parse a full stream-json blob (tests / replay)
# ---------------------------------------------------------------------------


def parse_stream_json(
    text: str,
    *,
    environment: str = "production",
    device_id: str | None = None,
    device_name: str | None = None,
) -> tuple["SessionIngest", CursorStreamDiagnostics]:
    """Parse a complete stream-json document into a ``SessionIngest``.

    Splits on newlines and feeds each line to a :class:`CursorStreamBuilder`.
    Used by tests and by CLI replay/debug paths. Live streaming should use the
    builder directly.
    """
    builder = CursorStreamBuilder(
        environment=environment,
        device_id=device_id,
        device_name=device_name,
    )
    for line in text.splitlines():
        builder.feed_line(line)
    return builder.build(), builder.diag
