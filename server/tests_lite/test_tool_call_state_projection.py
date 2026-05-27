"""Server-authoritative tool-call lifecycle projection.

The projection collapses three signals into ``EventResponse.tool_call_state``:
- a paired tool result event (→ completed)
- session lifecycle (closed → orphan calls drop)
- call age (>1h → orphan calls drop, even on open sessions)

Clients consume the field; they do not re-derive it.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from zerg.services.session_views import DROPPED_TOOL_AGE
from zerg.services.session_views import ToolCallState
from zerg.services.session_views import build_tool_call_state_map


def _event(
    event_id: int,
    role: str,
    *,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    timestamp: datetime,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=event_id,
        role=role,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        timestamp=timestamp,
    )


def test_paired_call_is_completed():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [
        _event(1, "assistant", tool_name="bash", tool_call_id="A", timestamp=now),
        _event(2, "tool", tool_call_id="A", timestamp=now + timedelta(seconds=1)),
    ]
    result = build_tool_call_state_map(events, session_closed=False, now=now)
    assert result == {1: ToolCallState.COMPLETED}


def test_unpaired_call_in_open_session_is_running():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [_event(1, "assistant", tool_name="bash", tool_call_id="A", timestamp=now)]
    result = build_tool_call_state_map(events, session_closed=False, now=now)
    assert result == {1: ToolCallState.RUNNING}


def test_unpaired_call_in_closed_session_is_dropped():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [_event(1, "assistant", tool_name="bash", tool_call_id="A", timestamp=now)]
    result = build_tool_call_state_map(events, session_closed=True, now=now)
    assert result == {1: ToolCallState.DROPPED}


def test_unpaired_old_call_in_open_session_is_dropped():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    old = now - DROPPED_TOOL_AGE - timedelta(seconds=1)
    events = [_event(1, "assistant", tool_name="bash", tool_call_id="A", timestamp=old)]
    result = build_tool_call_state_map(events, session_closed=False, now=now)
    assert result == {1: ToolCallState.DROPPED}


def test_fifo_pairing_when_tool_call_id_missing():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [
        _event(1, "assistant", tool_name="bash", timestamp=now),
        _event(2, "assistant", tool_name="bash", timestamp=now + timedelta(seconds=1)),
        _event(3, "tool", timestamp=now + timedelta(seconds=2)),
    ]
    result = build_tool_call_state_map(events, session_closed=False, now=now)
    assert result == {
        1: ToolCallState.COMPLETED,
        2: ToolCallState.RUNNING,
    }


def test_orphan_result_does_not_appear_in_map():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [_event(1, "tool", tool_call_id="A", timestamp=now)]
    assert build_tool_call_state_map(events, session_closed=False, now=now) == {}


def test_non_tool_assistant_message_excluded():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [_event(1, "assistant", tool_name=None, timestamp=now)]
    assert build_tool_call_state_map(events, session_closed=False, now=now) == {}


def test_parallel_calls_with_distinct_ids_pair_independently():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [
        _event(1, "assistant", tool_name="bash", tool_call_id="A", timestamp=now),
        _event(2, "assistant", tool_name="bash", tool_call_id="B", timestamp=now),
        _event(3, "tool", tool_call_id="B", timestamp=now + timedelta(seconds=1)),
    ]
    result = build_tool_call_state_map(events, session_closed=False, now=now)
    assert result == {1: ToolCallState.RUNNING, 2: ToolCallState.COMPLETED}


def test_closed_session_does_not_overwrite_completed():
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    events = [
        _event(1, "assistant", tool_name="bash", tool_call_id="A", timestamp=now),
        _event(2, "tool", tool_call_id="A", timestamp=now + timedelta(seconds=1)),
        _event(3, "assistant", tool_name="bash", tool_call_id="B", timestamp=now + timedelta(seconds=2)),
    ]
    result = build_tool_call_state_map(events, session_closed=True, now=now)
    assert result == {
        1: ToolCallState.COMPLETED,
        3: ToolCallState.DROPPED,
    }
