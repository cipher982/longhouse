"""Cursor agent transcript decoder (unmanaged ingest).

Reads a Cursor ``store.db`` content-addressed blob DAG and produces a
``SessionIngest`` with ordered ``EventIngest`` rows for Longhouse ingest.

Scope (v1): the *current* cursor-agent format (>= ~2026), where the root
snapshot node's ``field 1`` lists ordered ids of pure-JSON message blobs
(``{role, content:[...]}``) including ``tool-call`` / ``tool-result`` blocks
paired by ``toolCallId``. The legacy pre-2026 chunked format is detected and
reported as a typed unsupported gap; see
``docs/specs/cursor-transcript-format.md``.

This module is decode-only and side-effect free. It does not touch the
network or the Longhouse DB. Callers (CLI, ingest wiring) feed the resulting
``SessionIngest`` through the existing ``/api/agents`` ingest surface.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Iterator

if TYPE_CHECKING:
    from zerg.services.agents.models import EventIngest
    from zerg.services.agents.models import SessionIngest

PROVIDER = "cursor"

# Typed unsupported-gap reasons (surfaced to callers, not raised where possible).
GAP_LEGACY_CHUNKED = "cursor_legacy_chunked_format"
GAP_MISSING_ROOT = "cursor_missing_root"
GAP_EMPTY_SESSION = "cursor_empty_session"


@dataclass
class CursorDecodeDiagnostics:
    unsupported_gap: str | None = None
    unsupported_reason: str | None = None
    message_count: int = 0
    event_count: int = 0
    unknown_block_types: dict[str, int] = field(default_factory=dict)
    workspace: str | None = None
    model: str | None = None
    title: str | None = None
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    # "synthetic" for unmanaged store.db decode (no per-message timestamps;
    # inter-event spacing is carried by a synthetic monotonic clock, tightened
    # by per-tool executionTime durations when present). "real" for managed
    # stream-json ingest (see cursor_stream.py).
    timestamp_fidelity: str = "synthetic"


@dataclass
class CursorDecodeResult:
    session: SessionIngest | None
    diagnostics: CursorDecodeDiagnostics


# ---------------------------------------------------------------------------
# Protobuf wire parser (minimal, tolerant)
# ---------------------------------------------------------------------------


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while i < len(data):
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return result, i
    return result, i


def _parse_snapshot_fields(data: bytes) -> dict[int, list[Any]]:
    """Parse a snapshot node's protobuf fields.

    Only varint (wt 0) and length-delimited (wt 2) are needed for the root.
    Unknown wire types stop parsing gracefully (the format is undocumented
    and may drift); callers only need field 1.
    """
    out: dict[int, list[Any]] = {}
    i = 0
    n = len(data)
    while i < n:
        key, i = _read_varint(data, i)
        if key == 0:
            # illegal/padding; skip to avoid infinite loop
            continue
        fn = key >> 3
        wt = key & 7
        if wt == 2:
            ln, i = _read_varint(data, i)
            out.setdefault(fn, []).append(data[i : i + ln])
            i += ln
        elif wt == 0:
            val, i = _read_varint(data, i)
            out.setdefault(fn, []).append(val)
        else:
            # wt 1/3/4/5/6/7: not used by the root fields we care about.
            # Stop rather than risk desyncing on an undocumented schema.
            break
    return out


def _is_id(v: Any) -> bool:
    return isinstance(v, (bytes, bytearray)) and len(v) == 32


# ---------------------------------------------------------------------------
# Store reading
# ---------------------------------------------------------------------------


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open a cursor store read-only and WAL-aware.

    ``immutable=1`` deliberately ignores the ``-wal``/``-shm`` sidecars, so it
    sees only the checkpointed main file. cursor-agent writes in WAL mode and
    checkpoints on exit, which means ``immutable=1`` is fine for a cold
    (post-exit) store but sees an empty file — and raises ``no such table:
    meta`` — while the session is still live and the conversation is in the
    WAL. ``mode=ro`` reads the WAL (WAL readers don't block the writer) so the
    live-transcript tailer can decode an in-flight session. Fall back to
    ``immutable=1`` for a cold store whose ``-shm`` is gone/locked.
    """
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"file:{path}?immutable=1", uri=True)


def _load_meta_and_blobs(path: Path) -> tuple[dict[str, str], dict[bytes, bytes]]:
    con = _open_readonly(path)
    try:
        meta = dict(con.execute("select key, value from meta"))
        blobs = {bytes.fromhex(r[0]): r[1] for r in con.execute("select id, data from blobs")}
    finally:
        con.close()
    return meta, blobs


def _decode_meta_json(meta: dict[str, str]) -> dict[str, Any]:
    raw = meta.get("0")
    if raw is None:
        return {}
    try:
        return json.loads(bytes.fromhex(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # some stores may store plain json instead of hex
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}


def _load_meta_json_file(session_dir: Path) -> dict[str, Any]:
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text("utf-8"))
    except (ValueError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


def _workspace_from_snapshot(root_data: bytes) -> str | None:
    """Older sessions put a file:// URI as field 9 text on the root."""
    fields = _parse_snapshot_fields(root_data)
    f9 = fields.get(9, [])
    if not f9:
        return None
    v = f9[0]
    if isinstance(v, (bytes, bytearray)):
        try:
            s = v.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if s.startswith("file://"):
            return s[len("file://") :]
    return None


# ---------------------------------------------------------------------------
# Message -> event mapping
# ---------------------------------------------------------------------------


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _event_timestamp(start: datetime, end: datetime, index: int, total: int) -> datetime:
    """Synthesize a monotonic timestamp within [start, end].

    Cursor stores no per-message timestamps (see spec). We distribute events
    uniformly across the session window as a clearly-synthetic ordering
    carrier. When start == end or total <= 1, return start.
    """
    if total <= 1:
        return start
    span = end - start
    if span.total_seconds() <= 0:
        return start
    frac = index / (total - 1)
    return start + span * frac


_TOOL_DURATION_KEYS = ("executionTime", "localExecutionTimeMs")


def _tool_duration_ms(msg: dict[str, Any]) -> int | None:
    """Extract a per-tool wall duration (ms) from a tool-result message.

    Cursor stores ``executionTime`` / ``localExecutionTimeMs`` inside
    ``providerOptions.cursor.highLevelToolCallResult.output.success`` on
    tool-result blocks. These are durations, not absolute timestamps, but they
    let a burst-aware synthetic clock give tool calls realistic width instead
    of smearing every event uniformly across an idle-heavy session window.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    best: int | None = None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool-result":
            continue
        po = block.get("providerOptions") or {}
        cursor = po.get("cursor") or {}
        hl = cursor.get("highLevelToolCallResult") or {}
        out = hl.get("output") or {}
        success = out.get("success") or {}
        for key in _TOOL_DURATION_KEYS:
            val = success.get(key)
            if isinstance(val, (int, float)) and val > 0:
                cand = int(val)
                if best is None or cand > best:
                    best = cand
    return best


def _burst_aware_timestamps(
    start: datetime,
    end: datetime,
    messages: list[dict[str, Any]],
) -> list[datetime]:
    """Per-message synthetic timestamps tightened by real tool durations.

    Falls back to uniform spread across ``[start, end]`` when no tool-result
    block carries an ``executionTime`` (no new information). When durations are
    present, walks a cumulative clock from ``start`` advancing by each tool's
    real duration (and a small base delta for non-tool messages) so events
    cluster in the active burst instead of smearing across an idle-heavy
    ``[createdAt, updatedAtMs]`` window. The clock is not clamped to ``end``:
    ``ended_at`` still reflects the true last-updated time, but event spacing
    follows real tool widths.
    """
    total = len(messages)
    if total == 0:
        return []
    durations = [_tool_duration_ms(m) for m in messages]
    has_durations = any(d is not None for d in durations)
    if not has_durations:
        return [_event_timestamp(start, end, i, total) for i in range(total)]
    from datetime import timedelta

    base_delta = timedelta(milliseconds=10)
    clock = start
    out: list[datetime] = []
    for d in durations:
        out.append(clock)
        clock = clock + (timedelta(milliseconds=d) if d else base_delta)
    return out


def _map_message(
    msg: dict[str, Any],
    occurred_at: datetime,
    source_path: str,
    unknown_block_types: dict[str, int],
    event_cls: type,
) -> list[EventIngest]:
    role = msg.get("role") or "assistant"
    content = msg.get("content")
    # content may be a plain string (system messages)
    if isinstance(content, str):
        return [
            event_cls(
                role=role,
                content_text=content,
                timestamp=occurred_at,
                source_path=source_path,
                raw_json=json.dumps(msg, ensure_ascii=False),
            )
        ]
    if not isinstance(content, list):
        return []

    raw_json = json.dumps(msg, ensure_ascii=False)
    events: list[EventIngest] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            events.append(
                event_cls(
                    role=role,
                    content_text=_coerce_text(block.get("text")),
                    timestamp=occurred_at,
                    source_path=source_path,
                    raw_json=raw_json,
                )
            )
        elif btype == "reasoning":
            # Preserve reasoning as a first-class assistant event; raw_json
            # carries the full block (including providerOptions/modelName and
            # signature) so the timeline can distinguish it later.
            events.append(
                event_cls(
                    role="assistant",
                    content_text=_coerce_text(block.get("text")),
                    timestamp=occurred_at,
                    source_path=source_path,
                    raw_json=raw_json,
                )
            )
        elif btype == "redacted-reasoning":
            events.append(
                event_cls(
                    role="assistant",
                    content_text="",
                    timestamp=occurred_at,
                    source_path=source_path,
                    raw_json=raw_json,
                )
            )
        elif btype == "tool-call":
            args = block.get("args") or block.get("input") or {}
            events.append(
                event_cls(
                    role="assistant",
                    tool_name=block.get("toolName"),
                    tool_input_json=args if isinstance(args, dict) else {"value": args},
                    tool_call_id=block.get("toolCallId"),
                    timestamp=occurred_at,
                    source_path=source_path,
                    raw_json=raw_json,
                )
            )
        elif btype == "tool-result":
            events.append(
                event_cls(
                    role="tool",
                    tool_name=block.get("toolName"),
                    tool_output_text=_coerce_text(block.get("result")),
                    tool_call_id=block.get("toolCallId"),
                    timestamp=occurred_at,
                    source_path=source_path,
                    raw_json=raw_json,
                )
            )
        else:
            unknown_block_types[str(btype)] = unknown_block_types.get(str(btype), 0) + 1
            # Preserve unknown blocks as assistant events with raw_json so they
            # surface as yellow review items rather than being silently lost.
            events.append(
                event_cls(
                    role=role,
                    content_text="",
                    timestamp=occurred_at,
                    source_path=source_path,
                    raw_json=raw_json,
                )
            )
    return events


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decode_store_db(
    path: Path,
    *,
    environment: str = "production",
    device_id: str | None = None,
    device_name: str | None = None,
) -> CursorDecodeResult:
    """Decode a Cursor ``store.db`` into a ``SessionIngest`` + diagnostics.

    Returns a result with ``session=None`` and a typed ``unsupported_gap`` when
    the store is missing, empty, legacy, or unreadable. Never raises on schema
    drift; raises only on unrecoverable I/O errors.
    """
    from zerg.services.agents.models import EventIngest
    from zerg.services.agents.models import SessionIngest

    path = Path(path)
    session_dir = path.parent
    diag = CursorDecodeDiagnostics()
    if not path.exists():
        diag.unsupported_gap = GAP_MISSING_ROOT
        diag.unsupported_reason = f"store.db not found: {path}"
        return CursorDecodeResult(None, diag)

    meta, blobs = _load_meta_and_blobs(path)
    meta_json = _decode_meta_json(meta)
    root_hex = meta_json.get("latestRootBlobId")
    agent_id = meta_json.get("agentId")
    if not root_hex or not agent_id:
        diag.unsupported_gap = GAP_MISSING_ROOT
        diag.unsupported_reason = "meta[0] missing latestRootBlobId or agentId"
        return CursorDecodeResult(None, diag)

    root_id = bytes.fromhex(root_hex)
    root_data = blobs.get(root_id)
    if root_data is None:
        diag.unsupported_gap = GAP_MISSING_ROOT
        diag.unsupported_reason = "latestRootBlobId not present in blobs"
        return CursorDecodeResult(None, diag)

    fields = _parse_snapshot_fields(root_data)
    ordered_ids = [v for v in fields.get(1, []) if _is_id(v)]
    if not ordered_ids:
        diag.unsupported_gap = GAP_EMPTY_SESSION
        diag.unsupported_reason = "root snapshot has no field-1 message ids"
        return CursorDecodeResult(None, diag)

    # Session-level metadata
    meta_file = _load_meta_json_file(session_dir)
    created_at_ms = meta_json.get("createdAt") or meta_file.get("createdAtMs")
    updated_at_ms = meta_file.get("updatedAtMs") or meta_json.get("updatedAtMs") or created_at_ms
    diag.created_at_ms = created_at_ms
    diag.updated_at_ms = updated_at_ms
    diag.model = meta_json.get("lastUsedModel")
    diag.title = meta_json.get("name") or meta_file.get("title")
    diag.workspace = _workspace_from_snapshot(root_data)

    start = _ms_to_datetime(created_at_ms)
    end = _ms_to_datetime(updated_at_ms)
    if start is None:
        start = datetime.now(tz=timezone.utc)
    if end is None or end < start:
        end = start

    source_path = str(path)
    events: list[EventIngest] = []
    message_count = 0
    decoded_messages: list[dict[str, Any]] = []
    for bid in ordered_ids:
        data = blobs.get(bid)
        if data is None:
            continue
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # A field-1 entry that is not pure JSON => legacy chunked format.
            diag.unsupported_gap = GAP_LEGACY_CHUNKED
            diag.unsupported_reason = (
                "field-1 message blob is not pure JSON; legacy chunked format " "is not supported in v1 (see cursor-transcript-format.md)"
            )
            return CursorDecodeResult(None, diag)
        if not isinstance(msg, dict) or "role" not in msg:
            diag.unsupported_gap = GAP_LEGACY_CHUNKED
            diag.unsupported_reason = "field-1 blob is JSON but has no role key"
            return CursorDecodeResult(None, diag)
        message_count += 1
        decoded_messages.append(msg)

    occurred_times = _burst_aware_timestamps(start, end, decoded_messages)
    for index, msg in enumerate(decoded_messages):
        occurred_at = occurred_times[index]
        events.extend(_map_message(msg, occurred_at, source_path, diag.unknown_block_types, EventIngest))

    diag.message_count = message_count
    diag.event_count = len(events)

    workspace = diag.workspace
    session = SessionIngest(
        provider=PROVIDER,
        environment=environment,
        project=Path(workspace).name if workspace else None,
        cwd=workspace,
        started_at=start,
        ended_at=end if updated_at_ms else None,
        provider_session_id=str(agent_id),
        device_id=device_id,
        device_name=device_name,
        events=events,
    )
    return CursorDecodeResult(session, diag)


def _ms_to_datetime(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


@dataclass
class CursorSessionSummary:
    """Lightweight meta-only summary of a Cursor session (no event decode).

    Cheap enough for local_health discovery scans that run frequently. Use
    :func:`decode_store_db` for full event decoding.
    """

    store_path: Path
    agent_id: str
    title: str | None
    workspace: str | None
    model: str | None
    created_at_ms: int | None
    updated_at_ms: int | None
    legacy: bool
    state: str = "detached"
    control_path: str = "unmanaged"
    liveness_model: str = "transcript"


def peek_cursor_session(path: Path) -> CursorSessionSummary | None:
    """Read only enough metadata to surface a cursor session for discovery.

    Returns ``None`` when the store is missing or has no usable root. The
    ``legacy`` flag is set when the first root ``field 1`` message blob is not
    pure JSON (legacy chunked format) — full decode is then required to know
    more, which discovery deliberately does not do.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        meta, blobs = _load_meta_and_blobs(path)
    except sqlite3.Error:
        return None
    meta_json = _decode_meta_json(meta)
    root_hex = meta_json.get("latestRootBlobId")
    agent_id = meta_json.get("agentId")
    if not root_hex or not agent_id:
        return None
    root_id = bytes.fromhex(root_hex)
    root_data = blobs.get(root_id)
    if root_data is None:
        return None

    meta_file = _load_meta_json_file(path.parent)
    legacy = False
    fields = _parse_snapshot_fields(root_data)
    f1 = [v for v in fields.get(1, []) if _is_id(v)]
    if f1:
        first = blobs.get(f1[0])
        if first is not None:
            try:
                msg = json.loads(first.decode("utf-8"))
                legacy = not isinstance(msg, dict) or "role" not in msg
            except (ValueError, UnicodeDecodeError):
                legacy = True
    return CursorSessionSummary(
        store_path=path,
        agent_id=str(agent_id),
        title=meta_json.get("name") or meta_file.get("title"),
        workspace=_workspace_from_snapshot(root_data),
        model=meta_json.get("lastUsedModel"),
        created_at_ms=meta_json.get("createdAt") or meta_file.get("createdAtMs"),
        updated_at_ms=meta_file.get("updatedAtMs") or meta_json.get("updatedAtMs"),
        legacy=legacy,
    )


def iter_local_cursor_session_summaries(
    cursor_root: Path | None = None,
) -> Iterator[CursorSessionSummary]:
    """Yield lightweight summaries for every ``store.db`` under ~/.cursor/chats."""
    for store_path in iter_local_cursor_stores(cursor_root):
        summary = peek_cursor_session(store_path)
        if summary is not None:
            yield summary


def iter_local_cursor_stores(cursor_root: Path | None = None) -> Iterator[Path]:
    """Yield every ``store.db`` under ``~/.cursor/chats`` (unordered)."""
    root = cursor_root or (Path.home() / ".cursor" / "chats")
    if not root.exists():
        return
    yield from root.glob("*/*/store.db")


@dataclass
class CursorIngestResult:
    ingest: Any  # zerg.services.agents.IngestResult | None
    diagnostics: CursorDecodeDiagnostics


def ingest_cursor_store_db(
    db: Any,
    path: Path,
    *,
    environment: str = "production",
    device_id: str | None = None,
    device_name: str | None = None,
    chunk_size: int | None = None,
) -> CursorIngestResult:
    """Decode a Cursor ``store.db`` and ingest it through ``AgentsStore``.

    This is the unmanaged ingest seam: it reuses the canonical
    ``/api/agents`` store path (the same one the router uses) so cursor
    sessions land in Longhouse with no second ingest/query stack. When the
    decoder reports a typed unsupported gap (legacy format, missing root,
    empty session), no ingest is attempted and ``ingest`` is ``None``.
    """
    from zerg.services.agents.store import AgentsStore

    decoded = decode_store_db(
        path,
        environment=environment,
        device_id=device_id,
        device_name=device_name,
    )
    if decoded.session is None:
        return CursorIngestResult(ingest=None, diagnostics=decoded.diagnostics)
    store = AgentsStore(db)
    ingest_result = store.ingest_session(
        decoded.session,
        chunk_size=chunk_size,
        raw_source_archived=True,
    )
    return CursorIngestResult(ingest=ingest_result, diagnostics=decoded.diagnostics)
