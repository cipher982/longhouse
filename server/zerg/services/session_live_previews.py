from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import SessionLivePreview
from zerg.models.live_store import LiveSessionLivePreview
from zerg.provider_contract.provider_interaction_semantics import omp_agent_end_is_terminal
from zerg.services.provisional_events import EVENT_ORIGIN_LIVE_PROVISIONAL
from zerg.services.provisional_events import TranscriptPreview
from zerg.services.provisional_events import build_provisional_cursor
from zerg.services.provisional_events import build_provisional_key
from zerg.services.tool_presentation import call_text_for
from zerg.utils.time import normalize_utc

LIVE_PREVIEW_SOURCES = {
    "codex_bridge_live",
    "codex_console_live",
    "antigravity_acp",
    "antigravity_print",
    "claude_print",
    "cursor_print",
    "opencode_run",
    "opencode_server_bridge",
    "pi_print",
    "omp_print",
    "omp_helm_channel",
    "pi_helm_channel",
    "antigravity_helm_db",
    "claude_helm_channel",
    "codex_app_server",
}

# What a preview is, for a client that renders by kind. A preview with no kind is a reply (no tool name) or a tool (a tool name);
# ``subagent`` is a tool whose name is one of the provider subagent-spawning tools. ``thinking`` is never stored: it exists only on the
# live stream, and only for a connection that asked for it (``preview_kinds=thinking``), so a client that predates kinds cannot show
# reasoning text as the reply.
PREVIEW_KIND_REPLY = "reply"
PREVIEW_KIND_THINKING = "thinking"
PREVIEW_KIND_TOOL = "tool"
PREVIEW_KIND_SUBAGENT = "subagent"
PREVIEW_KINDS = (PREVIEW_KIND_REPLY, PREVIEW_KIND_THINKING, PREVIEW_KIND_TOOL, PREVIEW_KIND_SUBAGENT)
SUBAGENT_TOOL_NAMES = frozenset({"agent", "task", "spawn_agent", "invoke_subagent", "swarm"})
THINKING_MAX_CHARS = 8000  # the tail of the reasoning so far
TOOL_TAIL_MAX_LINES = 40  # a running tool's output is shown as its tail, never in full
TOOL_TAIL_MAX_CHARS = 4096


def preview_kind(tool_name: str | None) -> str:
    """The kind of a stored preview, from what it carries: no tool is a reply, a spawning tool is a subagent, any other tool is a tool."""

    name = (tool_name or "").strip()
    if not name:
        return PREVIEW_KIND_REPLY
    return PREVIEW_KIND_SUBAGENT if name.lower() in SUBAGENT_TOOL_NAMES else PREVIEW_KIND_TOOL


def bounded_tail(text: str | None) -> str | None:
    """The last lines of a running tool's output, at most 40 lines and 4 KiB."""

    if not text:
        return None
    if len(text) <= TOOL_TAIL_MAX_CHARS and text.count("\n") < TOOL_TAIL_MAX_LINES:
        return text  # within bounds: exactly as the provider wrote it
    tail = "\n".join(text.splitlines()[-TOOL_TAIL_MAX_LINES:])
    return tail[-TOOL_TAIL_MAX_CHARS:] or None


@dataclass(frozen=True)
class LivePreviewCandidate:
    session_id: UUID
    thread_id: str | None
    turn_key: str
    seq: int | None
    preview_text: str
    provisional_cursor: str
    provisional_complete: bool
    preview_observed_at: datetime
    source: str
    last_observation_id: str
    preview_role: str = "assistant"
    tool_name: str | None = None
    tool_input_json: dict | None = None
    tool_output_text: str | None = None
    tool_call_id: str | None = None
    tool_call_state: str | None = None
    kind: str = PREVIEW_KIND_REPLY
    # A thinking candidate carries one delta; the live stream accumulates it (LIVE_ITEM_MEMORY) and never stores it.
    text_is_delta: bool = False

    @property
    def effective_kind(self) -> str:
        return self.kind if self.kind != PREVIEW_KIND_REPLY else preview_kind(self.tool_name)


@dataclass(frozen=True)
class PublishedPreviewHead:
    """What a session's subscribers were last shown: the candidate's identity, not its text."""

    turn_key: str
    seq: int | None
    preview_observed_at: datetime
    last_observation_id: str


# Heads are per session and lane and tiny, but a Runtime Host lives for weeks. Past this
# many the least recently published head is forgotten; the cost of
# forgetting is one extra republish for a session or call that a resent batch names again.
MAX_PUBLISHED_PREVIEW_HEADS = 16384


def preview_lane(candidate: LivePreviewCandidate) -> str:
    """Which published head a candidate is ordered against.

    A client shows the reply as one piece of text and the thinking as another, so within either a preview of an older item
    must not step that text backwards: each is one lane per session. A tool call or a subagent call is a row of its own,
    keyed by the call. Ordering a call against the reply text lost it whenever the two crossed: the engine sends calls and
    reply text on different lanes (the durable outbox, the status slot), and over a real network the bridge merges whatever
    queued behind a post in flight. Measured 2026-11-12 with 150 ms of added engine-to-host delay: Antigravity Helm's tool
    start shared a post with newer reply text and was not published until the tool finished 6 s later, and Claude Helm's
    last reply snapshot before a tool call arrived behind the call and was refused as a straggler.
    """

    kind = candidate.effective_kind
    if kind in (PREVIEW_KIND_REPLY, PREVIEW_KIND_THINKING):
        return kind
    return f"call:{candidate.tool_call_id or candidate.turn_key}"


def admit_preview_publication(heads: OrderedDict[str, PublishedPreviewHead], candidate: LivePreviewCandidate) -> bool:
    """Whether fanning out ``candidate`` shows subscribers something new.

    The fast path publishes before catalogd applies the batch, so a batch the
    machine resends after a lost response arrives here a second time. This is
    the projection's own ordering rule (``upsert_live_session_live_preview``),
    applied to what was published instead of what was stored, one head per
    session and lane (``preview_lane``: the reply text, the thinking text, each
    tool or subagent call). It does not know
    when durable content supersedes a turn; only durable ingest does, and the
    client already retires a preview the durable rows replace. The same
    observation is never shown twice, and a preview that is not newer than the
    head of its lane, by seq within an item or by observation time across items, is a
    replay or a straggler and must not step the client backwards. A later
    update to the same item carries a higher seq and passes.
    """

    key = f"{candidate.session_id}\x1f{preview_lane(candidate)}"
    head = heads.get(key)
    if head is not None:
        if head.last_observation_id == candidate.last_observation_id:
            return False
        if not _candidate_should_replace(candidate, head):
            return False
    heads[key] = PublishedPreviewHead(
        turn_key=candidate.turn_key,
        seq=candidate.seq,
        preview_observed_at=candidate.preview_observed_at,
        last_observation_id=candidate.last_observation_id,
    )
    heads.move_to_end(key)
    while len(heads) > MAX_PUBLISHED_PREVIEW_HEADS:
        heads.popitem(last=False)
    return True


def live_preview_candidate_from_runtime_event(
    event: Any,
    *,
    observation_id: str,
) -> LivePreviewCandidate | None:
    payload = event.payload or {}
    if event.session_id is None:
        return None
    provider = (event.provider or "").strip().lower()
    if provider not in {"antigravity", "claude", "codex", "cursor", "opencode", "pi", "omp"}:
        return None
    source = (event.source or "").strip()
    if source.lower() not in LIVE_PREVIEW_SOURCES:
        return None
    if event.kind != "progress_signal":
        return None
    progress_kind = payload.get("progress_kind")
    if provider == "cursor" and source.lower() == "cursor_print" and progress_kind == "cursor_print_stream":
        return _cursor_print_preview_candidate(event, payload, observation_id=observation_id)
    if provider == "opencode" and source.lower() == "opencode_run" and progress_kind == "opencode_run_stream":
        return _opencode_run_preview_candidate(event, payload, observation_id=observation_id)
    if provider == "pi" and source.lower() == "pi_print" and progress_kind == "pi_print_stream":
        return _pi_print_preview_candidate(event, payload, observation_id=observation_id)
    if is_claude_print_live_text_event(event):
        return _claude_print_preview_candidate(event, payload, observation_id=observation_id)
    if provider == "claude" and source.lower() == "claude_print" and progress_kind == "claude_print_stream":
        return _claude_print_item_candidate(event, payload, observation_id=observation_id)
    if provider == "omp" and source.lower() == "omp_print" and progress_kind == "omp_print_stream":
        return _omp_print_preview_candidate(event, payload, observation_id=observation_id)
    if provider == "opencode" and source.lower() == "opencode_server_bridge" and progress_kind == "opencode_helm_stream":
        return _opencode_helm_preview_candidate(event, payload, observation_id=observation_id)
    if provider == "omp" and source.lower() == "omp_helm_channel" and progress_kind == "omp_helm_stream":
        return _omp_helm_preview_candidate(event, payload, observation_id=observation_id)
    if provider == "claude" and source.lower() == "claude_helm_channel" and progress_kind == "claude_helm_stream":
        return _omp_helm_preview_candidate(event, payload, observation_id=observation_id, source="claude_helm_channel")
    if provider == "pi" and source.lower() == "pi_helm_channel" and progress_kind == "pi_helm_stream":
        return _omp_helm_preview_candidate(event, payload, observation_id=observation_id, source="pi_helm_channel")
    if provider == "antigravity" and source.lower() == "antigravity_helm_db" and progress_kind == "antigravity_helm_stream":
        return _omp_helm_preview_candidate(event, payload, observation_id=observation_id, source="antigravity_helm_db")
    if provider == "codex" and source.lower() == "codex_app_server" and progress_kind == "codex_app_server_jsonrpc":
        return _codex_reasoning_candidate(event, payload, observation_id=observation_id)
    if progress_kind not in {"bridge_live_transcript_delta", "console_live_tool_item"}:
        return None

    is_tool = progress_kind == "console_live_tool_item"
    command = str(payload.get("command") or "").strip()
    output = str(payload.get("output") or "")
    preview_text = (output.strip() or command) if is_tool else str(payload.get("live_text") or "").strip()
    if not preview_text:
        return None

    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    item_id = _optional_str(payload.get("item_id"))
    turn_key = build_provisional_key(
        source=source,
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, item_id),
    )
    seq = _coerce_seq(payload.get("seq"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=preview_text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=bool(payload.get("turn_completed") or payload.get("completed")),
        preview_observed_at=observed_at,
        source=source,
        last_observation_id=observation_id,
        preview_role="assistant",
        tool_name=(_optional_str(payload.get("tool_name")) or "exec") if is_tool else None,
        tool_input_json={"command": command} if is_tool else None,
        tool_output_text=bounded_tail(output) if is_tool and output else None,
        tool_call_id=item_id if is_tool else None,
        tool_call_state=(
            _tool_call_state(
                payload.get("status"),
                completed=bool(payload.get("completed")),
            )
            if is_tool
            else None
        ),
    )


_CLAUDE_LIVE_TEXT_KINDS = {
    "claude_print": "claude_print_stream",
}


def is_claude_print_live_text_event(event: Any) -> bool:
    """A Claude runtime event that carries the reply text accumulated so far.

    The Console engine attaches ``live_text`` only to ``--include-partial-messages``
    text deltas and the closing ``message_stop``; every other stream line stays
    ordinary runtime progress evidence.
    """

    payload = event.payload or {}
    if (event.provider or "").strip().lower() != "claude" or event.kind != "progress_signal":
        return False
    if not isinstance(payload.get("live_text"), str):
        return False
    expected_kind = _CLAUDE_LIVE_TEXT_KINDS.get((event.source or "").strip().lower())
    return expected_kind is not None and payload.get("progress_kind") == expected_kind


def _claude_print_preview_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    if event.session_id is None:
        return None
    text = str(payload.get("live_text") or "").strip()
    if not text:
        return None
    source = (event.source or "").strip().lower()
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_key = build_provisional_key(
        source=source,
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(_optional_str(payload.get("turn_id")), _optional_str(payload.get("item_id"))),
    )
    seq = _coerce_seq(payload.get("seq"))
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=bool(payload.get("completed")),
        preview_observed_at=normalize_utc(event.occurred_at) or datetime.now(timezone.utc),
        source=source,
        last_observation_id=observation_id,
    )


class LiveItemMemory:
    """What the host remembers between runtime events so a live item can be drawn from one event.

    Two things arrive in pieces: reasoning (deltas, accumulated here and only here) and a Claude tool (its name and arguments come on one
    event, its output on a later one). Memory is per process and bounded; losing it costs a partial reasoning text or a tool called "tool",
    never a wrong item. Every operation is idempotent in the event's sequence number, because the same event is read by the live stream, the
    catalog and a resent batch.
    """

    MAX_SESSIONS = 256
    MAX_TOOLS = 512

    def __init__(self) -> None:
        self._thinking: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._tools: OrderedDict[tuple[str, str], tuple[str, dict[str, Any] | None]] = OrderedDict()

    def clear(self) -> None:
        self._thinking.clear()
        self._tools.clear()

    def remember_tool(self, session_id: Any, tool_id: str, name: str, args: dict[str, Any] | None) -> None:
        key = (str(session_id), tool_id)
        self._tools[key] = (name, args)
        self._tools.move_to_end(key)
        while len(self._tools) > self.MAX_TOOLS:
            self._tools.popitem(last=False)

    def tool(self, session_id: Any, tool_id: str) -> tuple[str, dict[str, Any] | None] | None:
        return self._tools.get((str(session_id), tool_id))

    def thinking_text(self, event: Any) -> str | None:
        """The reasoning so far, after applying this event once; ``None`` when the event carries none. Call exactly once per event, in order."""

        step = reasoning_step(event)
        if step is None:
            return None
        delta, item, reset, seq = step
        sid = str(event.session_id)
        state = self._thinking.get(sid)
        if state is None:
            state = {"text": "", "item": None, "seq": -1}
            self._thinking[sid] = state
        self._thinking.move_to_end(sid)
        while len(self._thinking) > self.MAX_SESSIONS:
            self._thinking.popitem(last=False)
        if seq is not None and seq <= state["seq"]:
            return state["text"] or None  # a replay of an event already applied
        if seq is not None:
            state["seq"] = seq
        if reset or (item is not None and item != state["item"]):
            state["text"] = ""
        if item is not None:
            state["item"] = item
        if delta:
            state["text"] = (state["text"] + delta)[-THINKING_MAX_CHARS:]
        return state["text"] or None


LIVE_ITEM_MEMORY = LiveItemMemory()


def reasoning_step(event: Any) -> tuple[str, str | None, bool, int | None] | None:
    """(delta, item id, starts a new block, sequence) for an event that carries reasoning text or opens a reasoning block, else ``None``."""

    payload = event.payload or {}
    raw = payload.get("event")
    if not isinstance(raw, dict) or event.kind != "progress_signal":
        return None
    provider, source, progress_kind = (
        (event.provider or "").strip().lower(),
        (event.source or "").strip().lower(),
        payload.get("progress_kind"),
    )
    seq = _coerce_seq(payload.get("seq"))
    if provider == "claude" and source == "claude_print" and progress_kind == "claude_print_stream" and raw.get("type") == "stream_event":
        inner = raw.get("event")
        if not isinstance(inner, dict):
            return None
        kind = inner.get("type")
        if kind == "message_start":
            return "", None, True, seq
        block = inner.get("content_block")
        if kind == "content_block_start" and isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}:
            return "", None, True, seq
        delta = inner.get("delta")
        if kind == "content_block_delta" and isinstance(delta, dict) and delta.get("type") == "thinking_delta":
            return str(delta.get("thinking") or ""), None, False, seq
        return None
    if provider == "codex" and source == "codex_app_server" and progress_kind == "codex_app_server_jsonrpc":
        method = str(raw.get("method") or "")
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        item = _optional_str(params.get("itemId"))
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            return str(params.get("delta") or ""), item, False, seq
        if method == "item/reasoning/summaryPartAdded":
            return "\n\n", item, False, seq
    return None


def _claude_print_item_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    """Claude Console stream-json lines that are not reply text: reasoning deltas, a tool or subagent call starting, its result."""

    raw = payload.get("event")
    if not isinstance(raw, dict) or event.session_id is None:
        return None
    raw_type = raw.get("type")
    seq = _coerce_seq(payload.get("seq"))
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    source = "claude_print"

    def candidate(item: str, text: str, **extra: Any) -> LivePreviewCandidate:
        turn_key = build_provisional_key(
            source=source, session_id=event.session_id, thread_id=thread_id, turn_id=_item_scoped_turn_id(turn_id, item)
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            preview_observed_at=observed_at,
            source=source,
            last_observation_id=observation_id,
            **{"provisional_complete": False, **extra},
        )

    step = reasoning_step(event)
    if step is not None:
        delta = step[0]
        return candidate("thinking", delta, kind=PREVIEW_KIND_THINKING, text_is_delta=True) if delta else None
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    content = message.get("content")
    if not isinstance(content, list):
        return None
    if raw_type == "assistant":
        calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and _optional_str(b.get("id"))]
        if not calls:
            return None
        call = calls[-1]
        tool_id, name = str(call["id"]), _optional_str(call.get("name")) or "tool"
        args = call.get("input") if isinstance(call.get("input"), dict) else None
        LIVE_ITEM_MEMORY.remember_tool(event.session_id, tool_id, name, args)
        kind = preview_kind(name)
        text = (_subagent_label(args) if kind == PREVIEW_KIND_SUBAGENT else None) or _tool_call_text(args) or name
        return candidate(tool_id, text, kind=kind, tool_name=name, tool_input_json=args, tool_call_id=tool_id, tool_call_state="running")
    if raw_type == "user":
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result" and _optional_str(b.get("tool_use_id"))]
        if not results:
            return None
        result = results[-1]
        tool_id = str(result["tool_use_id"])
        remembered = LIVE_ITEM_MEMORY.tool(event.session_id, tool_id)
        name = remembered[0] if remembered else "tool"
        args = remembered[1] if remembered else None
        output = (
            _native_tool_text(result.get("content"))
            if not isinstance(result.get("content"), list)
            else "".join(str(part.get("text") or "") for part in result["content"] if isinstance(part, dict))
        )
        kind = preview_kind(name)
        text = (_subagent_label(args) if kind == PREVIEW_KIND_SUBAGENT else None) or output.strip() or name
        return candidate(
            tool_id,
            text,
            kind=kind,
            tool_name=name,
            tool_input_json=args,
            tool_output_text=bounded_tail(output),
            tool_call_id=tool_id,
            tool_call_state="failed" if result.get("is_error") else "completed",
            provisional_complete=True,
        )
    return None


def _tool_call_text(args: dict[str, Any] | None) -> str | None:
    """The line a client shows for a call that has no output yet: the command, the path, or the arguments."""

    if not args:
        return None
    for key in ("command", "cmd", "file_path", "path", "pattern", "url", "query"):
        value = _optional_str(args.get(key))
        if value:
            return value[:400]
    return json.dumps(args, ensure_ascii=False, sort_keys=True)[:400]


def _codex_reasoning_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    """Codex Console reasoning: the app-server's ``item/reasoning/*Delta`` notifications, which the engine posts raw."""

    step = reasoning_step(event)
    if step is None or not step[0].strip() or event.session_id is None:
        return None
    seq = _coerce_seq(payload.get("seq"))
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_key = build_provisional_key(
        source="codex_app_server",
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(_optional_str(payload.get("turn_id")), "thinking"),
    )
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=step[0],
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=False,
        preview_observed_at=normalize_utc(event.occurred_at) or datetime.now(timezone.utc),
        source="codex_app_server",
        last_observation_id=observation_id,
        kind=PREVIEW_KIND_THINKING,
        text_is_delta=True,
    )


def _omp_helm_preview_candidate(
    event: Any,
    payload: dict[str, Any],
    *,
    observation_id: str,
    source: str = "omp_helm_channel",
) -> LivePreviewCandidate | None:
    if event.session_id is None:
        return None
    text = str(payload.get("live_text") or "").strip()
    if not text:
        return None
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id") or payload.get("run_id"))
    turn_key = build_provisional_key(
        source=source,
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=turn_id,
    )
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    seq = _coerce_seq(payload.get("seq"))
    # A Helm launcher that labels what its cumulative text is says so with `preview_kind` (thinking, tool, subagent) and, for a tool, the
    # `tool_name`, `tool_call_id`, `tool_input` and `tool_output` tail. A launcher that does not (every one today) sends reply text.
    labelled = _optional_str(payload.get("preview_kind"))
    kind = labelled if labelled in {PREVIEW_KIND_THINKING, PREVIEW_KIND_TOOL, PREVIEW_KIND_SUBAGENT} else PREVIEW_KIND_REPLY
    tool_name = _optional_str(payload.get("tool_name")) if kind in {PREVIEW_KIND_TOOL, PREVIEW_KIND_SUBAGENT} else None
    if kind != PREVIEW_KIND_REPLY:
        turn_key = build_provisional_key(
            source=source,
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=_item_scoped_turn_id(turn_id, _optional_str(payload.get("tool_call_id")) or kind),
        )
    tool_input = payload.get("tool_input")
    state = _optional_str(payload.get("tool_state"))
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=bool(payload.get("turn_completed") or payload.get("completed")),
        preview_observed_at=observed_at,
        source=source,
        last_observation_id=observation_id,
        kind=kind,
        tool_name=tool_name,
        tool_input_json=tool_input if isinstance(tool_input, dict) and tool_name else None,
        tool_output_text=bounded_tail(_optional_str(payload.get("tool_output"))) if tool_name else None,
        tool_call_id=_optional_str(payload.get("tool_call_id")) if tool_name else None,
        tool_call_state=(state if state in {"running", "completed", "failed"} else "running") if tool_name else None,
    )


def _opencode_helm_preview_candidate(
    event: Any,
    payload: dict[str, Any],
    *,
    observation_id: str,
) -> LivePreviewCandidate | None:
    """What the Helm bridge read from OpenCode's event stream: reply text, or (labelled with ``preview_kind``) reasoning, a tool or a subagent.

    ``live_text`` is the cumulative text of one assistant message and ``turn_id`` is that message's id, so a later message in a tool
    loop never overwrites an earlier one's preview. The labels are the ones the other Helm launchers send.
    """

    return _omp_helm_preview_candidate(event, payload, observation_id=observation_id, source="opencode_server_bridge")


def _labelled_console_preview_candidate(
    event: Any,
    payload: dict[str, Any],
    *,
    observation_id: str,
    source: str,
) -> LivePreviewCandidate | None:
    """A preview a Console engine posts through the status slot, labelled with what its text is.

    OMP and Pi Console strip the model's thinking out of the stream event they post, so the engine posts it separately as a slot preview
    (`preview_kind: thinking`, cumulative text, no `event`). It has the Helm launchers' shape, so the Helm reader builds it.
    """

    if isinstance(payload.get("event"), dict):
        return None
    if _optional_str(payload.get("preview_kind")) != PREVIEW_KIND_THINKING:
        return None
    return _omp_helm_preview_candidate(event, payload, observation_id=observation_id, source=source)


def _omp_print_preview_candidate(
    event: Any,
    payload: dict[str, Any],
    *,
    observation_id: str,
) -> LivePreviewCandidate | None:
    labelled = _labelled_console_preview_candidate(event, payload, observation_id=observation_id, source="omp_print")
    if labelled is not None:
        return labelled
    raw = payload.get("event")
    if not isinstance(raw, dict) or event.session_id is None:
        return None
    raw_type = str(raw.get("type") or "").strip()
    message = raw.get("message")
    role = message.get("role") if isinstance(message, dict) else None
    text = str(payload.get("live_text") or "").strip()
    if not text and role == "assistant" and isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
    if not text or _pi_tool_phase(raw_type) is not None:
        # A tool the provider is running (OMP print mode speaks the same tool_execution_* events as Pi). The engine
        # may stamp the turn's live text on these events too, so the event type decides, not an empty text.
        return _native_tool_candidate(
            event,
            payload,
            raw,
            raw_type=raw_type,
            source="omp_print",
            observation_id=observation_id,
            thread_id=_optional_str(payload.get("thread_id") or event.thread_id),
            turn_id=_optional_str(payload.get("turn_id")),
            seq=_coerce_seq(payload.get("seq")),
            observed_at=normalize_utc(event.occurred_at) or datetime.now(timezone.utc),
        )
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    message_index = _optional_str(payload.get("assistant_message_index"))
    turn_key = build_provisional_key(
        source="omp_print",
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, message_index),
    )
    stop_reason = _optional_str(message.get("stopReason")) if isinstance(message, dict) else None
    terminal = stop_reason in {"stop", "length"} or (raw_type == "agent_end" and omp_agent_end_is_terminal(raw))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    seq = _coerce_seq(payload.get("seq"))
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=terminal,
        preview_observed_at=observed_at,
        source="omp_print",
        last_observation_id=observation_id,
    )


def _cursor_print_preview_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    raw = payload.get("event")
    if not isinstance(raw, dict) or event.session_id is None:
        return None
    raw_type = str(raw.get("type") or "").strip()
    seq = _coerce_seq(payload.get("seq"))
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    if raw_type == "result" and raw.get("subtype") == "success":
        text = str(raw.get("result") or "").strip()
        if not text:
            return None
        turn_key = build_provisional_key(
            source="cursor_print",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=True,
            preview_observed_at=observed_at,
            source="cursor_print",
            last_observation_id=observation_id,
        )
    if raw_type == "assistant":
        message = raw.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        text = "".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        if not text:
            return None
        turn_key = build_provisional_key(
            source="cursor_print",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=False,
            preview_observed_at=observed_at,
            source="cursor_print",
            last_observation_id=observation_id,
        )
    if raw_type != "tool_call":
        return None
    call_id = _optional_str(raw.get("call_id"))
    call = raw.get("tool_call")
    if not isinstance(call, dict):
        return None
    tool_name, detail = _cursor_tool_detail(call)
    if detail is None:
        return None
    args = detail.get("args") if isinstance(detail.get("args"), dict) else {}
    result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
    success = result.get("success") if isinstance(result.get("success"), dict) else {}
    rejected = result.get("rejected") if isinstance(result.get("rejected"), dict) else {}
    command = str(args.get("command") or "").strip()
    output = _cursor_tool_output(success, rejected)
    subtype = str(raw.get("subtype") or "").strip()
    turn_key = build_provisional_key(
        source="cursor_print",
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, call_id),
    )
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=output.strip() or command or str(detail.get("description") or tool_name),
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=subtype == "completed",
        preview_observed_at=observed_at,
        source="cursor_print",
        last_observation_id=observation_id,
        preview_role="assistant",
        tool_name=tool_name,
        tool_input_json=dict(args) if args else None,
        tool_output_text=output or None,
        tool_call_id=call_id,
        tool_call_state="completed" if subtype == "completed" else "running",
    )


def _opencode_run_preview_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    raw = payload.get("event")
    if not isinstance(raw, dict) or event.session_id is None:
        return None
    part = raw.get("part") if isinstance(raw.get("part"), dict) else {}
    raw_type = str(raw.get("type") or "").strip()
    seq = _coerce_seq(payload.get("seq"))
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    if raw_type == "text" and str(part.get("type") or "") == "text":
        text = str(part.get("text") or "").strip()
        if not text:
            return None
        turn_key = build_provisional_key(
            source="opencode_run",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=False,
            preview_observed_at=observed_at,
            source="opencode_run",
            last_observation_id=observation_id,
        )
    if raw_type == "reasoning" and str(part.get("type") or "") == "reasoning":
        text = str(part.get("text") or "").strip()
        if not text:
            return None
        turn_key = build_provisional_key(
            source="opencode_run",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=_item_scoped_turn_id(turn_id, "thinking"),
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text[-THINKING_MAX_CHARS:],
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=False,
            preview_observed_at=observed_at,
            source="opencode_run",
            last_observation_id=observation_id,
            kind=PREVIEW_KIND_THINKING,
        )
    if raw_type != "tool_use" or str(part.get("type") or "") != "tool":
        return None
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool_name = str(part.get("tool") or "Tool")
    call_id = _optional_str(part.get("callID"))
    status = str(state.get("status") or "pending")
    tool_input = state.get("input") if isinstance(state.get("input"), dict) else None
    # A running tool's output so far is in the part's metadata; the finished output is in the state itself.
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    output = str(state.get("output") or metadata.get("output") or "")
    preview_text = output.strip() or json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True) or tool_name
    turn_key = build_provisional_key(
        source="opencode_run",
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, call_id),
    )
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=preview_text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=status in {"completed", "error"},
        preview_observed_at=observed_at,
        source="opencode_run",
        last_observation_id=observation_id,
        preview_role="assistant",
        tool_name=tool_name,
        tool_input_json=dict(tool_input) if tool_input else None,
        tool_output_text=output or None,
        tool_call_id=call_id,
        tool_call_state=("failed" if status == "error" else "completed" if status == "completed" else "running"),
    )


def _pi_print_preview_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    labelled = _labelled_console_preview_candidate(event, payload, observation_id=observation_id, source="pi_print")
    if labelled is not None:
        return labelled
    raw = payload.get("event")
    if not isinstance(raw, dict) or event.session_id is None:
        return None

    raw_type = str(raw.get("type") or "").strip()
    seq = _coerce_seq(payload.get("seq"))
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)

    if raw_type in {"message_update", "message_end"}:
        item_id = _optional_str(payload.get("item_id")) or _native_message_id(raw)
        text = str(payload.get("live_text") or _native_message_text(raw.get("message")) or "").strip()
        if not text:
            return None
        turn_key = build_provisional_key(
            source="pi_print",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=_item_scoped_turn_id(turn_id, item_id),
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=raw_type == "message_end" or bool(payload.get("completed")),
            preview_observed_at=observed_at,
            source="pi_print",
            last_observation_id=observation_id,
        )

    return _native_tool_candidate(
        event,
        payload,
        raw,
        raw_type=raw_type,
        source="pi_print",
        observation_id=observation_id,
        thread_id=thread_id,
        turn_id=turn_id,
        seq=seq,
        observed_at=observed_at,
    )


def _native_tool_candidate(
    event: Any,
    payload: dict[str, Any],
    raw: dict[str, Any],
    *,
    raw_type: str,
    source: str,
    observation_id: str,
    thread_id: str | None,
    turn_id: str | None,
    seq: int | None,
    observed_at: datetime,
) -> LivePreviewCandidate | None:
    """A tool the provider (Pi or OMP print mode) is running: its real name, arguments, and the output so far."""

    tool_phase = _pi_tool_phase(raw_type)
    if tool_phase is None:
        return None
    tool_call_id = _optional_str(_native_value(raw, "toolCallId", "tool_call_id"))
    if tool_call_id is None:
        # A stdout sequence is ordering evidence, never a native tool identity.
        return None
    tool_name = _optional_str(_native_value(raw, "toolName", "tool_name"))
    args = _native_value(raw, "args", "arguments")
    is_error = _native_bool(_native_value(raw, "isError", "is_error"))
    result = _native_value(raw, "result", "partialResult", "partial_result")
    output = _native_tool_text(result)
    if not output and is_error:
        output = "Tool failed"
    args_text = json.dumps(args, ensure_ascii=False, sort_keys=True) if args else ""
    kind = preview_kind(tool_name)
    label = _subagent_label(args) if kind == PREVIEW_KIND_SUBAGENT else None
    preview_text = label or output.strip() or args_text or tool_name or "Tool"
    turn_key = build_provisional_key(
        source=source,
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, tool_call_id),
    )
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=preview_text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=tool_phase == "completed",
        preview_observed_at=observed_at,
        source=source,
        last_observation_id=observation_id,
        preview_role="assistant",
        tool_name=tool_name,
        tool_input_json=dict(args) if isinstance(args, dict) else None,
        tool_output_text=bounded_tail(output),
        tool_call_id=tool_call_id,
        tool_call_state="failed" if is_error else tool_phase,
        kind=kind,
    )


def _subagent_label(args: Any) -> str | None:
    """What the user calls a spawned subagent: its description, its name, or the start of its task."""

    if not isinstance(args, dict):
        return None
    for key in ("description", "name"):
        value = _optional_str(args.get(key))
        if value:
            return value[:120]
    tasks = args.get("tasks")
    if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
        first = tasks[0]
        for key in ("name", "description", "task"):
            value = _optional_str(first.get(key))
            if value:
                return value[:120]
    for key in ("prompt", "message", "task"):
        value = _optional_str(args.get(key))
        if value:
            return value[:120]
    return None


def preview_payload_from_runtime_event(event: Any, *, observation_id: str) -> dict[str, Any] | None:
    """Build the immediate SSE shape from the same canonical preview candidate."""

    candidate = live_preview_candidate_from_runtime_event(event, observation_id=observation_id)
    if candidate is None:
        return None
    return {
        "event_id": candidate.seq or 0,
        "text": candidate.preview_text,
        "role": candidate.preview_role,
        "tool_name": candidate.tool_name,
        "tool_input_json": candidate.tool_input_json,
        "tool_output_text": candidate.tool_output_text,
        "tool_call_id": candidate.tool_call_id,
        "tool_call_state": candidate.tool_call_state,
        **(call_text_for(candidate.tool_name, candidate.tool_input_json) if candidate.tool_name else {}),
        "kind": candidate.effective_kind,
        "event_origin": EVENT_ORIGIN_LIVE_PROVISIONAL,
        "timestamp": candidate.preview_observed_at.isoformat().replace("+00:00", "Z"),
        "is_provisional": True,
        "is_complete": candidate.provisional_complete,
        "content_cursor": candidate.provisional_cursor,
        "is_stale": False,
        "stale_reason": None,
    }


def _cursor_tool_detail(call: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    for key, label in (
        ("shellToolCall", "Shell"),
        ("mcpToolCall", "MCP"),
        ("readToolCall", "Read"),
        ("writeToolCall", "Write"),
    ):
        value = call.get(key)
        if isinstance(value, dict):
            return label, value
    return "Tool", None


def _cursor_tool_output(success: dict[str, Any], rejected: dict[str, Any]) -> str:
    for key in ("stdout", "interleavedOutput", "content", "output", "text"):
        value = success.get(key)
        if isinstance(value, str) and value:
            return value
    reason = rejected.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return json.dumps(success, ensure_ascii=False, sort_keys=True) if success else ""


def _native_message_id(raw: dict[str, Any]) -> str | None:
    message = raw.get("message")
    if not isinstance(message, dict):
        return _optional_str(raw.get("messageId") or raw.get("message_id"))
    return _optional_str(message.get("id")) or _optional_str(raw.get("messageId") or raw.get("message_id"))


def _native_message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text")


def _native_value(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def _native_tool_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _native_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _pi_tool_phase(raw_type: str) -> str | None:
    normalized = raw_type.strip().lower().replace("-", "_")
    if normalized in {"tool_execution_start", "tool_call", "toolcall", "tool_start"}:
        return "running"
    if normalized in {"tool_execution_update", "tool_update"}:
        return "running"
    if normalized in {"tool_execution_end", "tool_result", "toolresult", "tool_end"}:
        return "completed"
    return None


def upsert_session_live_preview(db: Session, candidate: LivePreviewCandidate) -> bool:
    if candidate.kind == PREVIEW_KIND_THINKING:
        return False
    existing = db.get(SessionLivePreview, candidate.session_id)
    return _upsert_live_preview_row(db, candidate, existing, SessionLivePreview, session_id=candidate.session_id)


def upsert_live_session_live_preview(db: Session, candidate: LivePreviewCandidate) -> bool:
    if candidate.kind == PREVIEW_KIND_THINKING:
        return False  # reasoning exists only on the live stream, for a connection that asked for it
    existing = db.get(LiveSessionLivePreview, str(candidate.session_id))
    return _upsert_live_preview_row(db, candidate, existing, LiveSessionLivePreview, session_id=str(candidate.session_id))


def _upsert_live_preview_row(
    db: Session,
    candidate: LivePreviewCandidate,
    existing: Any,
    model,
    *,
    session_id: UUID | str,
) -> bool:
    now = datetime.now(timezone.utc)
    if existing is not None and existing.last_observation_id == candidate.last_observation_id:
        return False
    if existing is not None and existing.superseded_at is not None:
        if candidate.turn_key == existing.turn_key:
            # Durable content ended this turn; a late preview must not reopen it.
            return False
        superseded_at = normalize_utc(existing.superseded_at)
        candidate_at = normalize_utc(candidate.preview_observed_at)
        if superseded_at is not None and candidate_at is not None and candidate_at <= superseded_at:
            return False
    if existing is not None and not _candidate_should_replace(candidate, existing):
        return False

    if existing is None:
        db.add(
            model(
                session_id=session_id,
                thread_id=candidate.thread_id,
                turn_key=candidate.turn_key,
                seq=candidate.seq,
                preview_text=candidate.preview_text,
                preview_role=candidate.preview_role,
                tool_name=candidate.tool_name,
                tool_input_json=(json.dumps(candidate.tool_input_json) if candidate.tool_input_json is not None else None),
                tool_output_text=candidate.tool_output_text,
                tool_call_id=candidate.tool_call_id,
                tool_call_state=candidate.tool_call_state,
                provisional_cursor=candidate.provisional_cursor,
                provisional_complete=1 if candidate.provisional_complete else 0,
                event_origin=EVENT_ORIGIN_LIVE_PROVISIONAL,
                preview_observed_at=candidate.preview_observed_at,
                preview_updated_at=now,
                source=candidate.source,
                last_observation_id=candidate.last_observation_id,
            )
        )
        return True

    same_turn = candidate.turn_key == existing.turn_key
    existing.thread_id = candidate.thread_id
    existing.turn_key = candidate.turn_key
    existing.seq = candidate.seq
    existing.preview_text = candidate.preview_text
    existing.preview_role = candidate.preview_role
    if candidate.tool_name is not None or not same_turn:
        existing.tool_name = candidate.tool_name
    if candidate.tool_input_json is not None:
        existing.tool_input_json = json.dumps(candidate.tool_input_json)
    elif not same_turn:
        existing.tool_input_json = None
    if candidate.tool_output_text is not None or not same_turn:
        existing.tool_output_text = candidate.tool_output_text
    existing.tool_call_id = candidate.tool_call_id
    existing.tool_call_state = candidate.tool_call_state
    existing.provisional_cursor = candidate.provisional_cursor
    existing.provisional_complete = 1 if candidate.provisional_complete else 0
    existing.event_origin = EVENT_ORIGIN_LIVE_PROVISIONAL
    existing.preview_observed_at = candidate.preview_observed_at
    existing.preview_updated_at = now
    existing.source = candidate.source
    existing.last_observation_id = candidate.last_observation_id
    existing.superseded_at = None
    existing.superseded_by_event_id = None
    existing.superseded_reason = None
    return True


def supersede_session_live_preview(
    db: Session,
    *,
    session_id: UUID,
    durable_at: datetime | None,
    durable_event_id: int | None = None,
    reason: str = "superseded_by_durable",
) -> bool:
    row = db.get(SessionLivePreview, session_id)
    if row is None or row.superseded_at is not None:
        return False
    normalized_durable_at = normalize_utc(durable_at)
    preview_at = normalize_utc(row.preview_observed_at)
    if normalized_durable_at is None or preview_at is None or normalized_durable_at < preview_at:
        return False

    now = datetime.now(timezone.utc)
    row.superseded_at = normalized_durable_at
    row.preview_updated_at = now
    row.superseded_by_event_id = durable_event_id
    row.superseded_reason = reason
    return True


def load_session_live_preview_map(db: Session, session_ids: list[UUID]) -> dict[str, TranscriptPreview]:
    if not session_ids:
        return {}
    rows = (
        db.query(SessionLivePreview)
        .filter(SessionLivePreview.session_id.in_(session_ids))
        .filter(SessionLivePreview.superseded_at.is_(None))
        .all()
    )
    return preview_map_from_rows(rows)


def preview_map_from_rows(rows) -> dict[str, TranscriptPreview]:
    previews: dict[str, TranscriptPreview] = {}
    for row in rows:
        text = str(row.preview_text or "").strip()
        if not text:
            continue
        timestamp = normalize_utc(row.preview_observed_at)
        if timestamp is None:
            continue
        event_id = int(row.seq) if row.seq is not None else int(timestamp.timestamp() * 1000)
        previews[str(row.session_id)] = TranscriptPreview(
            event_id=event_id,
            text=text,
            event_origin=row.event_origin or EVENT_ORIGIN_LIVE_PROVISIONAL,
            timestamp=timestamp,
            provisional_cursor=row.provisional_cursor,
            provisional_complete=bool(row.provisional_complete),
            role=row.preview_role or "assistant",
            tool_name=row.tool_name,
            tool_input_json=json.loads(row.tool_input_json) if row.tool_input_json else None,
            tool_output_text=row.tool_output_text,
            tool_call_id=row.tool_call_id,
            tool_call_state=row.tool_call_state,
            kind=preview_kind(row.tool_name),
        )
    return previews


def candidate_supersedes(candidate: LivePreviewCandidate, other: LivePreviewCandidate) -> bool:
    """Of two previews of one lane in one batch, whether ``candidate`` is the one to show (the projection's ordering rule)."""

    return _candidate_should_replace(candidate, other)


def _candidate_should_replace(
    candidate: LivePreviewCandidate, existing: SessionLivePreview | PublishedPreviewHead | LivePreviewCandidate
) -> bool:
    candidate_at = normalize_utc(candidate.preview_observed_at) or datetime.min.replace(tzinfo=timezone.utc)
    existing_at = normalize_utc(existing.preview_observed_at) or datetime.min.replace(tzinfo=timezone.utc)
    if candidate.turn_key != existing.turn_key:
        return candidate_at >= existing_at
    if candidate.seq is not None and existing.seq is not None:
        if candidate.seq != existing.seq:
            return candidate.seq > existing.seq
        return candidate_at >= existing_at
    if candidate.seq is not None and existing.seq is None:
        return True
    if candidate.seq is None and existing.seq is not None:
        return False
    return candidate_at >= existing_at


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _item_scoped_turn_id(turn_id: str | None, item_id: str | None) -> str | None:
    if not item_id:
        return turn_id
    return f"{turn_id or 'unknown-turn'}#{item_id}"


def _coerce_seq(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _tool_call_state(value: Any, *, completed: bool) -> str:
    status = str(value or "").strip().lower()
    if completed or status in {"completed", "failed", "cancelled"}:
        return "completed"
    return "running"
