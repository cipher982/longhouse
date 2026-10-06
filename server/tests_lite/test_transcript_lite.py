import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.services.storage_v2_workspace as workspace_module
from zerg.services.transcript_lite import has_structured_failure
from zerg.services.transcript_lite import lite_projection
from zerg.services.transcript_lite import tool_output_preview
from zerg.services.transcript_lite import truncate_tool_input
from zerg.storage_v2.contracts import RenderDetailCursor
from zerg.storage_v2.contracts import decode_render_detail_cursor_token
from zerg.storage_v2.contracts import render_detail_cursor_token


def test_short_output_is_sent_unchanged():
    text = "Chunk ID: a1\nWall time: 0.4 seconds\nProcess exited with code 0\nOutput:\nok\n"
    assert tool_output_preview(text) == text


def test_long_wrapped_output_keeps_header_and_the_rows_visible_lines():
    body = "\n".join(f"line {n}" for n in range(1, 41))
    text = f"Chunk ID: a1\nWall time: 3.2 seconds\nProcess exited with code 2\nOutput:\n{body}"
    preview = tool_output_preview(text)
    # The header survives, so exit code and wall time still parse on the client.
    assert preview.startswith("Chunk ID: a1\nWall time: 3.2 seconds\nProcess exited with code 2\nOutput:\n")
    shown = preview.split("Output:\n", 1)[1].split("\n")
    assert shown == ["line 1", "line 2", "… 30 more lines …", *[f"line {n}" for n in range(33, 41)]]


def test_unwrapped_output_is_bounded_like_the_collapsed_row():
    text = "\n".join(str(n) for n in range(100))
    lines = tool_output_preview(text).split("\n")
    assert lines[:3] == ["0", "1", "… 90 more lines …"]
    assert lines[-1] == "99"
    assert len(lines) == 11


def test_one_long_line_is_cut_one_past_the_client_cap():
    preview = tool_output_preview("x" * 10_000)
    assert len(preview) == 4097


def test_structured_failure_matches_the_client_rule():
    assert has_structured_failure('{"ok": false, "error": "boom"}')
    assert has_structured_failure('{"exit_code": 3}')
    assert has_structured_failure("[tool error] permission denied")
    assert not has_structured_failure('{"ok": true}')
    assert not has_structured_failure('{"exit_code": true}')
    assert not has_structured_failure("plain text")


def test_tool_input_truncation_reports_what_it_cut():
    value, cut = truncate_tool_input({"command": "ls", "content": "y" * 1000, "paths": list(range(80))})
    assert cut is True
    assert value["command"] == "ls"
    assert len(value["content"]) == 300
    assert len(value["paths"]) == 50
    assert truncate_tool_input({"command": "ls"}) == ({"command": "ls"}, False)


def _item(event_id, *, timestamp="2026-10-06T12:00:00+00:00", **event):
    base = {
        "id": event_id,
        "cursor": f"cursor-{event_id}",
        "role": "assistant",
        "content_text": None,
        "interaction_kind": None,
        "raw_content_text": None,
        "input_origin": None,
        "turn_end": None,
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "tool_output_truncated": False,
        "tool_output_original_chars": None,
        "tool_call_id": None,
        "tool_presentation": None,
        "timestamp": timestamp,
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
    base.update(event)
    return {
        "kind": "event",
        "session_id": "s-1",
        "timestamp": timestamp,
        "event": base,
        "action": None,
        "continued_from_session_id": None,
        "continuation_kind": None,
        "origin_label": None,
        "parent_origin_label": None,
        "parent_continuation_kind": None,
        "branched_from_event_id": None,
    }


def test_lite_projection_keeps_text_and_sends_presentations_once():
    presentation = {"version": 2, "tool_name": "read", "label": "Read", "tool_input_json": None, "children": []}
    edit_input = {"file_path": "a.py", "new_string": "z" * 2000}
    projection = {
        "focus_session_id": "s-1",
        "generation_id": "g-1",
        "next_cursor": "cursor-1",
        "has_more": True,
        "items": [
            _item("1", role="user", content_text="please read both files"),
            _item("2", tool_name="Read", tool_input_json={"path": "a.py"}, tool_presentation=presentation, tool_call_id="c2"),
            _item("3", tool_name="Read", tool_input_json={"path": "b.py"}, tool_presentation=presentation, tool_call_id="c3"),
            _item(
                "4",
                tool_name="Edit",
                tool_input_json=edit_input,
                tool_presentation={"version": 2, "label": "Edit", "tool_input_json": edit_input, "children": []},
                in_active_context=False,
                is_head_branch=False,
            ),
            _item("6", tool_name="Bash", tool_input_json={"command": "cat <<EOF\n" + "q" * 900}),
            _item("5", role="tool", tool_output_text='{"ok": false, "detail": "' + "e" * 5000 + '"}', tool_call_id="c3"),
        ],
    }

    lite = lite_projection(projection)

    assert lite["detail"] == "lite"
    assert lite["next_cursor"] == "cursor-1"
    user, read_a, read_b, edit, bash, result = (item["event"] for item in lite["items"])
    assert lite["items"][0] == {"kind": "event", "timestamp": "2026-10-06T12:00:00+00:00", "event": user}
    assert user == {"id": "1", "cursor": "cursor-1", "role": "user", "content_text": "please read both files"}
    # Two reads share one presentation entry.
    assert read_a["tool_presentation_ref"] == read_b["tool_presentation_ref"]
    assert len(lite["tool_presentations"]) == 2
    assert "tool_input_json" not in lite["tool_presentations"][read_a["tool_presentation_ref"]]
    # The edit's presented input is its own input, so it travels once, and
    # whole: the collapsed row counts its lines.
    assert edit["tool_presentation_input"] == "same"
    assert "tool_input_truncated" not in edit
    assert edit["tool_input_json"] == edit_input
    assert edit["in_active_context"] is False and edit["is_head_branch"] is False
    assert bash["tool_input_truncated"] is True
    assert len(bash["tool_input_json"]["command"]) == 300
    # A cut structured failure still reads as failed without its full text.
    assert result["tool_output_truncated"] is True
    assert result["tool_output_failed"] is True
    assert result["tool_output_original_chars"] > 5000


class _Catalog:
    async def call(self, method, params, *, timeout_seconds=None):
        return {"found": True, "commit_seq": "8", "session": {"owner_id": "42", "updated_at": "2026-10-06T12:00:00Z"}}


def _session(session_id):
    return SimpleNamespace(
        provider="claude",
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True),
        model_dump=lambda **_kwargs: {"id": str(session_id), "lifecycle": "open", "capabilities": {}},
    )


def _page_event(event_id, **fields):
    event = {
        "event_id": event_id,
        "cursor": f"cursor-{event_id}",
        "timestamp": "2026-10-06T12:00:00+00:00",
        "role": "assistant",
        "content_text": None,
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "tool_call_id": None,
        "branch_kind": None,
    }
    event.update(fields)
    return event


@pytest.mark.asyncio
async def test_workspace_lite_detail_drops_thread_session_copies(monkeypatch):
    session_id = uuid4()

    async def read_page(**_kwargs):
        return {
            "generation_id": str(uuid4()),
            "events": [
                _page_event("e1", role="user", content_text="hi"),
                _page_event("e2", tool_name="Bash", tool_input_json={"command": "ls"}),
            ],
            "next_cursor": None,
            "has_more": False,
            "total": 2,
        }

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _sid, **_kw: (_session(session_id), None, "7"))
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)

    full = await workspace_module.build_storage_v2_workspace(session_id=session_id, owner_id=42, branch_mode="head", limit=50)
    lite = await workspace_module.build_storage_v2_workspace(
        session_id=session_id, owner_id=42, branch_mode="head", limit=50, detail="lite"
    )

    assert full["thread"]["sessions"] and "detail" not in full["projection"]
    assert "sessions" not in lite["thread"]
    assert lite["thread"]["head_session_id"] == str(session_id)
    assert lite["projection"]["detail"] == "lite"
    assert [item["event"]["id"] for item in lite["projection"]["items"]] == ["e1", "e2"]
    assert lite["session"] == full["session"]


@pytest.mark.asyncio
async def test_delta_page_that_reaches_the_newest_event_counts_as_the_tail(monkeypatch):
    session_id = uuid4()
    seen = {}

    async def read_page(**kwargs):
        assert kwargs["anchor"] == "start" and kwargs["cursor"] == "cursor-e1"
        return {"generation_id": str(uuid4()), "events": [_page_event("e2")], "next_cursor": None, "has_more": False, "total": 9}

    def turn_ends(facts, events, *, page_is_tail):
        seen["page_is_tail"] = page_is_tail
        return {}

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _sid, **_kw: (_session(session_id), None, "7"))
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)
    monkeypatch.setattr(workspace_module, "turn_ends_by_event", turn_ends)

    await workspace_module.build_storage_v2_workspace(
        session_id=session_id, owner_id=42, branch_mode="head", limit=50, cursor="cursor-e1", anchor="start"
    )

    assert seen["page_is_tail"] is True


def _cursor(session_id, *, subordinal):
    return render_detail_cursor_token(
        RenderDetailCursor(
            session_id=session_id,
            render_generation=uuid4(),
            order_time_us=1_790_000_000_000_000,
            machine_id="cinder",
            provider="claude",
            opaque_source_id="source-1",
            source_epoch=uuid4(),
            source_position=812,
            event_subordinal=subordinal,
        )
    )


@pytest.mark.asyncio
async def test_event_bodies_read_each_event_from_just_past_its_cursor(monkeypatch):
    session_id = uuid4()
    wanted = _cursor(session_id, subordinal=1)
    stale = _cursor(session_id, subordinal=4)
    calls = []

    async def read_page(**kwargs):
        calls.append(kwargs)
        bound = decode_render_detail_cursor_token(kwargs["cursor"])
        if bound.event_subordinal == 2:
            event = _page_event("e9", tool_name="Bash", tool_input_json={"command": "pytest -q"}, tool_output_text="x" * 9000)
            event["cursor"] = wanted
            return {"events": [event]}
        # Nothing at the stale cursor any more: the newest earlier event is another one.
        return {"events": [_page_event("other")]}

    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _sid, **_kw: (_session(session_id), None, "7"))
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)

    result = await workspace_module.read_storage_v2_event_bodies(session_id=session_id, owner_id=42, cursors=[wanted, stale, wanted])

    assert [event["id"] for event in result["events"]] == ["e9"]
    assert result["events"][0]["tool_output_text"] == "x" * 9000
    assert result["events"][0]["tool_presentation"]["source_tool_name"] == "Bash"
    assert result["missing"] == [stale]
    assert len(calls) == 2
    assert all(call["anchor"] == "tail" and call["limit"] == 1 and call["branch_mode"] == "all" for call in calls)
    assert decode_render_detail_cursor_token(calls[0]["cursor"]).event_subordinal == 2


@pytest.mark.asyncio
async def test_event_bodies_refuse_a_cursor_from_another_session(monkeypatch):
    session_id = uuid4()
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _sid, **_kw: (_session(session_id), None, "7"))

    with pytest.raises(workspace_module.HTTPException) as excinfo:
        await workspace_module.read_storage_v2_event_bodies(session_id=session_id, owner_id=42, cursors=[_cursor(uuid4(), subordinal=0)])

    assert excinfo.value.status_code == 422
