"""Storage-v2 event filters must apply before the page window, not after.

Why this exists: ``get_session_detail(max_events=3, roles="assistant")`` against
a real 1286-event session returned ``events: []`` next to ``total: 1286``. The
workspace builder has no filter predicate, so a page-sized window whose first
rows happen to be user or system events left nothing to return. The endpoint has
to over-collect and then trim.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from zerg.routers import agents_sessions


def _event(index: int, role: str) -> dict:
    return {
        "id": f"legacy:{index}",
        "cursor": f"cursor-{index}",
        "role": role,
        "content_text": f"{role} message {index}",
        "raw_content_text": None,
        "input_origin": None,
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "tool_output_truncated": False,
        "tool_output_original_chars": None,
        "tool_call_id": None,
        "timestamp": "2026-07-12T12:00:00+00:00",
        "in_active_context": True,
        "branch_id": None,
        "is_head_branch": True,
        "event_origin": "durable",
        "provisional_state": None,
        "provisional_cursor": None,
        "provisional_complete": False,
        "reconciled_event_id": None,
        "tool_call_state": None,
        "media_refs": [],
    }


def _workspace_factory(session_id, roles: list[str]):
    """A session whose opening events are all non-assistant, like the real one."""

    events = [_event(index, role) for index, role in enumerate(roles)]

    async def build_workspace(**kwargs):
        limit = int(kwargs["limit"])
        page = events[:limit]
        return {
            "thread": {
                "root_session_id": str(session_id),
                "head_session_id": str(session_id),
                "sessions": [],
            },
            "projection": {
                "root_session_id": str(session_id),
                "focus_session_id": str(session_id),
                "head_session_id": str(session_id),
                "path_session_ids": [str(session_id)],
                "items": [
                    {
                        "kind": "event",
                        "session_id": str(session_id),
                        "timestamp": event["timestamp"],
                        "event": event,
                        "action": None,
                        "continued_from_session_id": None,
                        "continuation_kind": None,
                        "origin_label": None,
                        "parent_origin_label": None,
                        "parent_continuation_kind": None,
                        "branched_from_event_id": None,
                    }
                    for event in page
                ],
                "total": len(events),
                "page_offset": 0,
                "branch_mode": "head",
                "abandoned_events": 0,
                "generation_id": str(uuid4()),
                "next_cursor": page[-1]["cursor"] if page else None,
                "has_more": len(page) < len(events),
            },
        }

    return build_workspace


async def _get_events(session_id, *, roles, limit, anchor="start"):
    return await agents_sessions.get_session_events(
        session_id=session_id,
        thread_id=None,
        roles=roles,
        tool_name=None,
        query=None,
        context_mode="forensic",
        branch_mode="head",
        anchor=anchor,
        limit=limit,
        offset=0,
        cursor=None,
        db=None,
        _auth=SimpleNamespace(owner_id=42),
        _single=None,
    )


@pytest.mark.asyncio
async def test_role_filter_reaches_past_the_page_window(monkeypatch):
    """The reported failure: three-event window, no assistant events in it."""
    session_id = uuid4()
    roles = ["user", "system", "user"] + ["assistant"] * 10
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "build_storage_v2_workspace", _workspace_factory(session_id, roles))

    result = await _get_events(session_id, roles="assistant", limit=3)

    assert len(result.events) == 3, "a post-window filter would return zero here"
    assert {event.role for event in result.events} == {"assistant"}


@pytest.mark.asyncio
async def test_unfiltered_reads_do_not_over_collect(monkeypatch):
    """Without a filter there is nothing to over-collect for."""
    session_id = uuid4()
    seen = []
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)

    inner = _workspace_factory(session_id, ["user"] + ["assistant"] * 20)

    async def build_workspace(**kwargs):
        seen.append(int(kwargs["limit"]))
        return await inner(**kwargs)

    monkeypatch.setattr(agents_sessions, "build_storage_v2_workspace", build_workspace)

    result = await _get_events(session_id, roles=None, limit=5)

    assert seen == [5]
    assert len(result.events) == 5


@pytest.mark.asyncio
async def test_trimmed_page_resumes_after_the_last_returned_event(monkeypatch):
    """A cursor pointing past the scan would skip matches the trim discarded."""
    session_id = uuid4()
    roles = ["user"] + ["assistant"] * 20
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "build_storage_v2_workspace", _workspace_factory(session_id, roles))

    result = await _get_events(session_id, roles="assistant", limit=2)

    assert [event.id for event in result.events] == ["legacy:1", "legacy:2"]
    assert result.has_more is True
    assert result.next_cursor == "cursor-2"


@pytest.mark.asyncio
async def test_tail_anchor_keeps_the_newest_matches(monkeypatch):
    session_id = uuid4()
    roles = ["user"] + ["assistant"] * 20
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "build_storage_v2_workspace", _workspace_factory(session_id, roles))

    result = await _get_events(session_id, roles="assistant", limit=2, anchor="tail")

    assert [event.id for event in result.events] == ["legacy:19", "legacy:20"]
    assert result.has_more is True


@pytest.mark.asyncio
async def test_search_branch_projects_instead_of_relying_on_response_model(monkeypatch):
    """The query branch has its own return and must narrow it explicitly.

    Returning the browser listing here still produced a slim-looking payload,
    because FastAPI coerces it to the response model by field name — so `title`
    and `searchable`, which exist only on the machine projection, silently came
    back as their defaults instead of the values they were meant to carry.
    """
    from datetime import datetime

    from zerg.services.session_views import SessionResponse

    session = SessionResponse(
        id=str(uuid4()),
        provider="claude",
        project="zerg",
        started_at=datetime(2026, 8, 1, 15, 7, 18),
        user_messages=2,
        assistant_messages=26,
        tool_calls=154,
        timeline_title="Implement Group C Spec",
        thread_root_session_id=str(uuid4()),
        thread_head_session_id=str(uuid4()),
        thread_continuation_count=1,
        capabilities={"live_control_available": False, "host_reattach_available": False, "reply_to_live_session_available": False},
        session_state={
            "mode": "shadow",
            "disposition": {"state": "open"},
            "activity": {"state": "unknown"},
            "control": {
                "ownership": "unowned",
                "connection": "not_applicable",
                "actions": {
                    "start_turn": {"state": "unavailable", "reason": "not_console"},
                    "send_input": {"state": "unavailable", "reason": "observe_only"},
                    "interrupt": {"state": "unavailable", "reason": "observe_only"},
                    "terminate": {"state": "unavailable", "reason": "observe_only"},
                    "reattach": {"state": "unavailable", "reason": "observe_only"},
                    "resume": {"state": "unavailable", "reason": "observe_only"},
                },
            },
            "transcript": {"convergence": "current", "searchable": True},
            "host": {"state": "online"},
            "presentation": {},
        },
        runtime_display={
            "truth_tier": "none", "signal_tier": "none", "state": None, "tone": "inactive",
            "headline": "Inactive", "detail": None, "phase_label": "Inactive", "compact_tool_label": None,
            "is_live": False, "is_executing": False, "needs_attention": False, "is_idle": False,
            "is_stalled": False, "is_managed_local_truth": False, "has_signal": False,
            "control_path": "unmanaged", "activity_recency": "none", "lifecycle": "open",
            "host_state": "online", "terminal_reason": None,
        },
        timeline_card={
            "ownership": {"label": "Unmanaged", "tone": "neutral"},
            "status": {"label": "Activity unknown", "tone": "inactive", "seen_at_prefix": "Checked"},
            "border_tone": "inactive",
        },
    )

    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)

    async def fake_search(**_kwargs):
        return [session]

    monkeypatch.setattr(agents_sessions, "search_storage_v2_sessions", fake_search)

    result = await agents_sessions.list_sessions(
        project="zerg", provider=None, environment=None, include_test=False,
        hide_autonomous=True, include_automation=False, device_id=None, days_back=7,
        query="group c", limit=20, offset=0, sort=None, mode=None,
        context_mode="forensic", db=None,
        _auth=SimpleNamespace(owner_id=42), _single=None,
    )

    served = result.sessions[0]
    assert served.title == "Implement Group C Spec", "the search branch must project, not defer to the response model"
    assert served.searchable is True
