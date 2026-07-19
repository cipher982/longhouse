import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi import Response

from zerg.routers import agents_sessions
from zerg.services.catalog_read_gateway import CatalogReadError


def _workspace(session_id):
    event = {
        "id": "legacy:41",
        "cursor": "cursor-41",
        "role": "assistant",
        "content_text": "migration complete",
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
    projection = {
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
        ],
        "total": 1,
        "page_offset": 0,
        "branch_mode": "head",
        "abandoned_events": 0,
        "generation_id": str(uuid4()),
        "next_cursor": "cursor-41",
        "has_more": True,
    }
    return {
        "thread": {
            "root_session_id": str(session_id),
            "head_session_id": str(session_id),
            "sessions": [],
        },
        "projection": projection,
    }


def test_machine_session_detail_uses_token_owner_and_marks_canonical_serve(monkeypatch):
    session_id = uuid4()
    projected = SimpleNamespace(id=str(session_id))
    call = {}
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)

    def read(requested, *, owner_id):
        call.update(session_id=requested, owner_id=owner_id)
        return projected, "provider-thread", "31"

    monkeypatch.setattr(agents_sessions, "read_live_catalog_session", read)
    response = Response()

    result = agents_sessions.get_session(
        session_id=session_id,
        response=response,
        db=None,
        _auth=SimpleNamespace(owner_id=42),
        _single=None,
        owner_id=None,
    )

    assert result is projected
    assert call == {"session_id": session_id, "owner_id": 42}
    assert response.headers["X-Catalog-Commit-Seq"] == "31"
    assert response.headers["X-Provider-Session-ID"] == "provider-thread"
    assert response.headers["X-Session-State-Serve"] == "canonical_session_detail"


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("canonical_owner_required", 401),
        ("shadow_fact_head_limit_exceeded", 409),
        ("invalid_catalog_snapshot", 503),
    ],
)
def test_machine_session_detail_maps_fail_closed_catalog_errors(monkeypatch, code, expected_status):
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)

    def fail(*_args, **_kwargs):
        raise CatalogReadError(code, "canonical detail unavailable")

    monkeypatch.setattr(agents_sessions, "read_live_catalog_session", fail)

    with pytest.raises(HTTPException) as raised:
        agents_sessions.get_session(
            session_id=uuid4(),
            response=Response(),
            db=None,
            _auth=SimpleNamespace(owner_id=42),
            _single=None,
            owner_id=None,
        )

    assert raised.value.status_code == expected_status
    assert raised.value.detail["code"] == code


@pytest.mark.asyncio
async def test_machine_session_reads_use_storage_v2_without_legacy_db(monkeypatch):
    session_id = uuid4()
    calls = []

    async def build_workspace(**kwargs):
        calls.append(kwargs)
        return _workspace(session_id)

    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "build_storage_v2_workspace", build_workspace)
    for endpoint in (
        agents_sessions.get_session_thread,
        agents_sessions.get_session_events,
        agents_sessions.get_session_projection,
        agents_sessions.session_tail,
    ):
        dependency = inspect.signature(endpoint).parameters["db"].default.dependency
        assert dependency is agents_sessions.machine_session_read_db_dependency
    assert list(agents_sessions._session_detail_db()) == [None]
    auth = SimpleNamespace(owner_id=42)

    thread = await agents_sessions.get_session_thread(
        session_id=session_id,
        response=Response(),
        db=None,
        _auth=auth,
        _single=None,
        owner_id=None,
    )
    events = await agents_sessions.get_session_events(
        session_id=session_id,
        thread_id=None,
        roles="assistant",
        tool_name=None,
        query="complete",
        context_mode="forensic",
        branch_mode="head",
        anchor="start",
        limit=20,
        offset=0,
        cursor=None,
        db=None,
        _auth=auth,
        _single=None,
    )
    projection = await agents_sessions.get_session_projection(
        session_id=session_id,
        response=Response(),
        thread_id=None,
        branch_mode="head",
        anchor="tail",
        limit=20,
        offset=0,
        cursor=None,
        db=None,
        _auth=auth,
        _single=None,
    )
    tail = await agents_sessions.session_tail(
        session_id=session_id,
        limit=20,
        db=None,
        _auth=auth,
        _single=None,
    )

    assert thread["head_session_id"] == str(session_id)
    assert events.events[0].id == "legacy:41"
    assert events.next_cursor == "cursor-41"
    assert projection["items"][0]["event"]["content_text"] == "migration complete"
    assert tail["events"] == [
        {
            "id": "legacy:41",
            "role": "assistant",
            "content": "migration complete",
            "tool_name": None,
            "timestamp": "2026-07-12T12:00:00+00:00",
        }
    ]
    assert [call["anchor"] for call in calls if "anchor" in call] == ["start", "tail", "tail"]
    assert all(call["owner_id"] == 42 for call in calls)


@pytest.mark.asyncio
async def test_storage_v2_machine_reads_reject_legacy_offset(monkeypatch):
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    with pytest.raises(HTTPException) as exc_info:
        await agents_sessions.get_session_events(
            session_id=uuid4(),
            thread_id=None,
            roles=None,
            tool_name=None,
            query=None,
            context_mode="forensic",
            branch_mode="head",
            anchor="start",
            limit=20,
            offset=1,
            cursor=None,
            db=None,
            _auth=SimpleNamespace(owner_id=42),
            _single=None,
        )
    assert exc_info.value.status_code == 400
    assert "uses cursor" in exc_info.value.detail
