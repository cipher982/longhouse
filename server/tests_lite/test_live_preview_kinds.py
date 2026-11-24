"""Live previews carry a kind (reply, thinking, tool, subagent); thinking never reaches a client that did not ask for it."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest

from zerg.routers import runtime as runtime_router
from zerg.routers import timeline as timeline_mod
from zerg.services import session_live_previews as lp
from zerg.services.session_runtime import RuntimeEventIngest

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _event(provider: str, source: str, progress_kind: str, raw: dict, *, seq: int, session_id=None, **payload) -> RuntimeEventIngest:
    sid = session_id or uuid4()
    return RuntimeEventIngest(
        runtime_key=f"{provider}:{sid}",
        session_id=sid,
        provider=provider,
        device_id="cinder",
        source=source,
        kind="progress_signal",
        occurred_at=NOW,
        dedupe_key=f"{source}:{sid}:{seq}",
        payload={"progress_kind": progress_kind, "seq": seq, "thread_id": "t1", "turn_id": "turn-1", "event": raw, **payload},
    )


def _claude(raw: dict, seq: int, sid) -> RuntimeEventIngest:
    return _event("claude", "claude_print", "claude_print_stream", raw, seq=seq, session_id=sid)


def _thinking_delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": text}},
    }


@pytest.fixture(autouse=True)
def _fresh_memory():
    lp.LIVE_ITEM_MEMORY.clear()
    yield
    lp.LIVE_ITEM_MEMORY.clear()


def test_kind_follows_what_a_stored_preview_carries() -> None:
    assert lp.preview_kind(None) == "reply"
    assert lp.preview_kind("Bash") == "tool"
    for name in ("Agent", "Task", "task", "spawn_agent", "invoke_subagent", "swarm"):
        assert lp.preview_kind(name) == "subagent"


def test_a_tool_tail_is_bounded_by_lines_and_bytes() -> None:
    text = "\n".join(f"line {i}" for i in range(500))
    tail = lp.bounded_tail(text)
    assert tail and tail.splitlines()[-1] == "line 499" and len(tail.splitlines()) <= lp.TOOL_TAIL_MAX_LINES
    assert len(lp.bounded_tail("x" * 100_000) or "") <= lp.TOOL_TAIL_MAX_CHARS


def test_claude_reasoning_deltas_accumulate_once_and_a_new_block_starts_fresh() -> None:
    sid = uuid4()
    one, two = _claude(_thinking_delta("alpha "), 1, sid), _claude(_thinking_delta("beta"), 2, sid)
    assert lp.LIVE_ITEM_MEMORY.thinking_text(one) == "alpha "
    assert lp.LIVE_ITEM_MEMORY.thinking_text(two) == "alpha beta"
    assert lp.LIVE_ITEM_MEMORY.thinking_text(two) == "alpha beta"  # the same event read again (a resent batch) adds nothing
    restart = _claude({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m2"}}}, 3, sid)
    lp.LIVE_ITEM_MEMORY.thinking_text(restart)
    assert lp.LIVE_ITEM_MEMORY.thinking_text(_claude(_thinking_delta("next"), 4, sid)) == "next"


def test_the_published_thinking_preview_has_the_thinking_kind_and_the_cumulative_text() -> None:
    sid = uuid4()
    runtime_router.LIVE_ITEM_MEMORY.thinking_text(_claude(_thinking_delta("so far "), 1, sid))
    event = _claude(_thinking_delta("and more"), 2, sid)
    text = runtime_router.LIVE_ITEM_MEMORY.thinking_text(event)
    preview = runtime_router._live_transcript_preview_payload(event, now=NOW, thinking_text=text)
    assert preview and preview["kind"] == "thinking" and preview["text"] == "so far and more" and preview["tool_name"] is None


def test_thinking_is_never_stored_as_the_session_preview() -> None:
    event = _claude(_thinking_delta("secret"), 1, uuid4())
    candidate = lp.live_preview_candidate_from_runtime_event(event, observation_id="o1")
    assert candidate and candidate.kind == "thinking" and candidate.text_is_delta
    assert lp.upsert_session_live_preview(None, candidate) is False  # type: ignore[arg-type]
    assert lp.upsert_live_session_live_preview(None, candidate) is False  # type: ignore[arg-type]


def test_a_claude_tool_call_is_a_running_tool_with_its_real_name_then_completes_with_its_output() -> None:
    sid = uuid4()
    call = _claude(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "sleep 5; echo hi"}}]},
        },
        5,
        sid,
    )
    running = runtime_router._live_transcript_preview_payload(call, now=NOW)
    assert running and (running["kind"], running["tool_name"], running["tool_call_state"], running["text"]) == (
        "tool",
        "Bash",
        "running",
        "sleep 5; echo hi",
    )
    result = _claude({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "hi\n"}]}}, 6, sid)
    done = runtime_router._live_transcript_preview_payload(result, now=NOW)
    assert done and (done["kind"], done["tool_name"], done["tool_call_state"], done["tool_output_text"]) == (
        "tool",
        "Bash",
        "completed",
        "hi\n",
    )
    assert done["tool_call_id"] == running["tool_call_id"] == "tu1"


def test_a_claude_agent_call_is_a_subagent_with_its_label() -> None:
    call = _claude(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "tu9", "name": "Agent", "input": {"description": "ttruth sub", "prompt": "x"}}]
            },
        },
        7,
        uuid4(),
    )
    preview = runtime_router._live_transcript_preview_payload(call, now=NOW)
    assert preview and (preview["kind"], preview["tool_name"], preview["text"]) == ("subagent", "Agent", "ttruth sub")


def test_omp_print_tool_events_are_tools_and_a_task_tool_is_a_subagent() -> None:
    sid = uuid4()

    def omp(raw: dict, seq: int) -> RuntimeEventIngest:
        return _event("omp", "omp_print", "omp_print_stream", raw, seq=seq, session_id=sid)

    start = omp({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "echo a"}}, 1)
    update = omp(
        {"type": "tool_execution_update", "toolCallId": "c1", "toolName": "bash", "partialResult": {"content": [{"text": "a"}]}}, 2
    )
    first = runtime_router._live_transcript_preview_payload(start, now=NOW)
    later = runtime_router._live_transcript_preview_payload(update, now=NOW)
    assert first and later and first["kind"] == later["kind"] == "tool" and first["tool_name"] == "bash"
    task = omp(
        {"type": "tool_execution_start", "toolCallId": "c2", "toolName": "task", "args": {"tasks": [{"name": "TtSub", "task": "do"}]}}, 3
    )
    sub = runtime_router._live_transcript_preview_payload(task, now=NOW)
    assert sub and (sub["kind"], sub["text"]) == ("subagent", "TtSub")


def test_codex_console_reasoning_is_thinking_and_its_tool_keeps_the_exec_name_only_when_the_engine_names_none() -> None:
    sid = uuid4()

    def codex(method: str, delta: str, seq: int) -> RuntimeEventIngest:
        return _event(
            "codex",
            "codex_app_server",
            "codex_app_server_jsonrpc",
            {"method": method, "params": {"itemId": "r1", "delta": delta}},
            seq=seq,
            session_id=sid,
        )

    event = codex("item/reasoning/summaryTextDelta", "think", 1)
    text = lp.LIVE_ITEM_MEMORY.thinking_text(event)
    preview = runtime_router._live_transcript_preview_payload(event, now=NOW, thinking_text=text)
    assert preview and preview["kind"] == "thinking" and preview["text"] == "think"
    tool = RuntimeEventIngest(
        runtime_key=f"codex:{sid}",
        session_id=sid,
        provider="codex",
        device_id="cinder",
        source="codex_console_live",
        kind="progress_signal",
        occurred_at=NOW,
        dedupe_key="t1",
        payload={"progress_kind": "console_live_tool_item", "command": "ls", "output": "a", "item_id": "i1", "seq": 3},
    )
    assert runtime_router._live_transcript_preview_payload(tool, now=NOW)["tool_name"] == "exec"  # type: ignore[index]
    named = tool.model_copy(update={"payload": {**tool.payload, "tool_name": "Shell"}})
    assert runtime_router._live_transcript_preview_payload(named, now=NOW)["tool_name"] == "Shell"  # type: ignore[index]


def test_reply_text_previews_keep_the_reply_kind_and_the_old_fields() -> None:
    event = _event("claude", "claude_print", "claude_print_stream", {"type": "stream_event"}, seq=9, live_text="hello there", item_id="a1")
    preview = runtime_router._live_transcript_preview_payload(event, now=NOW)
    assert preview and preview["kind"] == "reply" and preview["text"] == "hello there" and preview["tool_name"] is None


# ---- the wire: a connection that did not ask for thinking never receives it -------------------------------------------------------


class _Disconnect:
    def __init__(self, cycles: int) -> None:
        self.cycles = cycles

    async def is_disconnected(self) -> bool:
        self.cycles -= 1
        return self.cycles < 0


def _frames(accepted: frozenset[str], previews: list[dict]) -> list[dict]:
    from zerg.services.session_pubsub import publish_session_transcript_preview_update
    from zerg.services.session_pubsub import reset_pubsub_for_test

    reset_pubsub_for_test()
    session_id = uuid4()
    for i, preview in enumerate(previews):
        publish_session_transcript_preview_update(
            session_id=str(session_id), provider="claude", source="claude_print", transcript_preview={"event_id": i + 1, **preview}
        )

    async def run() -> list[dict]:
        out = []
        async for frame in timeline_mod._live_catalog_workspace_stream(
            _Disconnect(len(previews) + 3),
            session_id=session_id,
            skip_initial=False,
            last_event_id=0,
            accepted_preview_kinds=accepted,
        ):
            out.append(frame)
        return out

    return [__import__("json").loads(f["data"]) for f in asyncio.run(run()) if f.get("event") == "workspace_changed"]


def _preview(kind: str | None, text: str, **extra) -> dict:
    body = {
        "text": text,
        "role": "assistant",
        "tool_name": None,
        "event_origin": "live_provisional",
        "timestamp": NOW.isoformat().replace("+00:00", "Z"),
        "is_provisional": True,
        "is_complete": False,
        "is_stale": False,
        **extra,
    }
    if kind is not None:
        body["kind"] = kind
    return body


def test_a_connection_that_did_not_ask_never_receives_thinking_text() -> None:
    frames = _frames(frozenset(), [_preview("thinking", "reasoning words"), _preview("reply", "the reply")])
    shown = [f["transcript_preview"]["text"] for f in frames if f.get("transcript_preview")]
    assert "reasoning words" not in shown and "the reply" in shown
    assert "reasoning words" not in str(frames)


def test_a_connection_that_asked_receives_thinking_with_its_kind() -> None:
    frames = _frames(frozenset({"thinking"}), [_preview("thinking", "reasoning words"), _preview("reply", "the reply")])
    kinds = {f["transcript_preview"]["text"]: f["transcript_preview"].get("kind") for f in frames if f.get("transcript_preview")}
    assert kinds == {"reasoning words": "thinking", "the reply": "reply"}


def test_old_shapes_still_pass_unchanged_with_no_kind_at_all() -> None:
    frames = _frames(
        frozenset(), [_preview(None, "legacy reply"), _preview(None, "", tool_name="Bash", tool_call_state="running") | {"text": "ls"}]
    )
    texts = [f["transcript_preview"]["text"] for f in frames if f.get("transcript_preview")]
    assert texts == ["legacy reply", "ls"]


def test_a_helm_launcher_that_labels_its_preview_is_published_by_kind_and_one_that_does_not_is_a_reply() -> None:
    sid = uuid4()

    def helm(**payload) -> RuntimeEventIngest:
        return RuntimeEventIngest(
            runtime_key=f"claude:{sid}",
            session_id=sid,
            provider="claude",
            device_id="cinder",
            source="claude_helm_channel",
            kind="progress_signal",
            occurred_at=NOW,
            dedupe_key=f"h:{payload['seq']}",
            payload={"progress_kind": "claude_helm_stream", "turn_id": "turn-1", "live_text": "words", **payload},
        )

    plain = runtime_router._live_transcript_preview_payload(helm(seq=1), now=NOW)
    assert plain and plain["kind"] == "reply" and plain["tool_name"] is None
    thinking = runtime_router._live_transcript_preview_payload(helm(seq=2, preview_kind="thinking"), now=NOW)
    assert thinking and (thinking["kind"], thinking["text"]) == ("thinking", "words")
    tool = runtime_router._live_transcript_preview_payload(
        helm(seq=3, preview_kind="tool", tool_name="Bash", tool_call_id="c1", tool_output="a\nb", tool_state="running"), now=NOW
    )
    assert tool and (tool["kind"], tool["tool_name"], tool["tool_output_text"], tool["tool_call_state"]) == (
        "tool",
        "Bash",
        "a\nb",
        "running",
    )


def test_omp_print_task_event_with_live_text_is_a_subagent_not_a_reply() -> None:
    raw = {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "task", "args": {"tasks": [{"name": "TtSub"}]}}
    event = _event("omp", "omp_print", "omp_print_stream", raw, seq=3, live_text="earlier reply text")
    cand = lp.live_preview_candidate_from_runtime_event(event, observation_id="o1")
    assert cand is not None and cand.kind == "subagent" and cand.preview_text == "TtSub"


def test_codex_helm_tool_and_collab_items_are_a_tool_and_a_subagent_under_their_own_names() -> None:
    sid = uuid4()

    def helm(name: str, seq: int, **payload) -> RuntimeEventIngest:
        return RuntimeEventIngest(
            runtime_key=f"codex:{sid}",
            session_id=sid,
            provider="codex",
            device_id="cinder",
            source="codex_bridge_live",
            kind="progress_signal",
            occurred_at=NOW,
            dedupe_key=f"b:{seq}",
            payload={
                "progress_kind": "console_live_tool_item",
                "turn_id": "turn-1",
                "item_id": f"i{seq}",
                "tool_name": name,
                "seq": seq,
                **payload,
            },
        )

    shell = runtime_router._live_transcript_preview_payload(helm("shell", 1, command="ls", output="a\nb", status="inProgress"), now=NOW)
    assert shell and (shell["kind"], shell["tool_name"], shell["tool_output_text"], shell["tool_call_state"]) == (
        "tool",
        "shell",
        "a\nb",
        "running",
    )
    spawn = runtime_router._live_transcript_preview_payload(helm("spawn_agent", 2, command="count the files", status="inProgress"), now=NOW)
    assert spawn and (spawn["kind"], spawn["tool_name"], spawn["text"]) == ("subagent", "spawn_agent", "count the files")
    done = runtime_router._live_transcript_preview_payload(
        helm("shell", 3, command="ls", output="a", status="completed", completed=True), now=NOW
    )
    assert done and done["tool_call_state"] == "completed"


def test_opencode_helm_labelled_previews_are_thinking_tools_and_subagents_and_unlabelled_text_is_the_reply() -> None:
    sid = uuid4()

    def helm(**payload) -> RuntimeEventIngest:
        return RuntimeEventIngest(
            runtime_key=f"opencode:{sid}",
            session_id=sid,
            provider="opencode",
            device_id="cinder",
            source="opencode_server_bridge",
            kind="progress_signal",
            occurred_at=NOW,
            dedupe_key=f"o:{payload['seq']}",
            payload={"progress_kind": "opencode_helm_stream", "turn_id": "msg_a", "live_text": "words", **payload},
        )

    def preview(**payload):
        return runtime_router._live_transcript_preview_payload(helm(**payload), now=NOW)

    plain = preview(seq=1)
    assert plain and plain["kind"] == "reply" and plain["tool_name"] is None
    thinking = preview(seq=2, preview_kind="thinking")
    assert thinking and (thinking["kind"], thinking["text"]) == ("thinking", "words")
    tool = preview(seq=3, preview_kind="tool", tool_name="bash", tool_call_id="c1", tool_output="a\nb", tool_state="running")
    assert tool and (tool["kind"], tool["tool_name"], tool["tool_output_text"]) == ("tool", "bash", "a\nb")
    sub = preview(seq=4, preview_kind="subagent", tool_name="task", tool_call_id="c2", tool_state="running")
    assert sub and (sub["kind"], sub["tool_name"]) == ("subagent", "task")


def test_opencode_run_reasoning_is_thinking_and_a_running_tool_shows_the_output_so_far() -> None:
    def run(raw: dict, seq: int) -> RuntimeEventIngest:
        return _event("opencode", "opencode_run", "opencode_run_stream", raw, seq=seq)

    reasoning = run({"type": "reasoning", "part": {"id": "p1", "type": "reasoning", "text": "weigh options"}}, 1)
    cand = lp.live_preview_candidate_from_runtime_event(reasoning, observation_id="o1")
    assert cand is not None and cand.kind == "thinking" and cand.preview_text == "weigh options" and not cand.text_is_delta
    part = {
        "id": "p2",
        "type": "tool",
        "tool": "bash",
        "callID": "call_1",
        "state": {"status": "running", "input": {"command": "ls"}, "metadata": {"output": "one\ntwo"}},
    }
    tool = lp.live_preview_candidate_from_runtime_event(run({"type": "tool_use", "part": part}, 2), observation_id="o2")
    assert tool is not None and tool.tool_output_text == "one\ntwo" and tool.tool_call_state == "running"
    assert (tool.effective_kind, tool.tool_name) == ("tool", "bash")
