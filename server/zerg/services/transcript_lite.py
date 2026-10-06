"""Lite transcript pages: conversation in full, tool bodies as previews.

A transcript page spends most of its bytes on tool input and output that the
reader only sees after expanding a row. ``detail=lite`` keeps every event and
all conversation text, but sends each tool body as exactly what its collapsed
row shows, and each tool presentation once per page. Clients rebuild the full
event shape (see web ``hydrateLiteProjection``) and fetch a full body from
``/event-bodies`` when a row is expanded.

The output preview mirrors the web collapsed row (``getToolOutputPreview`` in
``timelineModel.ts``): the wrapper header is kept verbatim so exit code and
wall time still parse, then the trimmed output is bounded to its first 2 and
last 8 lines and 4096 characters. Change one only together with the other.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from zerg.services.tool_presentation import DEFAULT_RULES_PATH
from zerg.services.tool_presentation import _load_rules

DETAIL_FULL = "full"
DETAIL_LITE = "lite"
DETAIL_MODES = frozenset({DETAIL_FULL, DETAIL_LITE})

# Mirrors TOOL_OUTPUT_PREVIEW_* in web/src/shared/session/model/timelineModel.ts.
_PREVIEW_HEAD_LINES = 2
_PREVIEW_TAIL_LINES = 8
_PREVIEW_MAX_CHARS = 4096
# Enough of a tool input to derive its one-line summary (a command, a path).
_INPUT_STRING_MAX_CHARS = 300
_INPUT_LIST_MAX_ITEMS = 50

_WALL_TIME = re.compile(r"^Wall time: [\d.]+ seconds$")
_EXIT_CODE = re.compile(r"^Process exited with code \d+$")
_TOKEN_COUNT = re.compile(r"^Original token count: \d+$")
# Mirrors activityCategory's edit rule in timelineModel.ts. An edit's collapsed
# row shows a +/- line count computed from its whole input, so edit inputs are
# never cut.
_EDIT_IDENTITY = re.compile(r"\b(edit|edited|write|patch|replace|notebook|create file|write to file)\b")

# Event fields whose value carries information only when it differs from these.
_EVENT_DEFAULTS: dict[str, Any] = {
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
_ITEM_DEFAULTS: dict[str, Any] = {
    "action": None,
    "continued_from_session_id": None,
    "continuation_kind": None,
    "origin_label": None,
    "parent_origin_label": None,
    "parent_continuation_kind": None,
    "branched_from_event_id": None,
}


def _split_wrapper(text: str) -> tuple[list[str], str] | None:
    """Split a Longhouse command wrapper into header lines and output, as the web parser does."""
    lines = text.split("\n")
    index = 0
    saw_metadata = False
    if index < len(lines) and lines[index].startswith("Chunk ID: "):
        saw_metadata, index = True, index + 1
    if index < len(lines) and _WALL_TIME.match(lines[index]):
        saw_metadata, index = True, index + 1
    if index < len(lines) and _EXIT_CODE.match(lines[index]):
        saw_metadata, index = True, index + 1
    if index < len(lines) and _TOKEN_COUNT.match(lines[index]):
        saw_metadata, index = True, index + 1
    if index >= len(lines) or lines[index] != "Output:" or not saw_metadata:
        return None
    return lines[: index + 1], "\n".join(lines[index + 1 :])


def _bound(text: str) -> str:
    lines = text.split("\n")
    if len(lines) > _PREVIEW_HEAD_LINES + _PREVIEW_TAIL_LINES + 1:
        elided = len(lines) - _PREVIEW_HEAD_LINES - _PREVIEW_TAIL_LINES
        lines = [*lines[:_PREVIEW_HEAD_LINES], f"… {elided} more lines …", *lines[-_PREVIEW_TAIL_LINES:]]
    bounded = "\n".join(lines)
    # One character past the cap makes the client apply its own "truncated" marker.
    return bounded[: _PREVIEW_MAX_CHARS + 1]


def tool_output_preview(text: str) -> str:
    """Return the shortest output that renders the same collapsed preview on the web."""
    normalized = text.replace("\r\n", "\n")
    wrapper = _split_wrapper(normalized)
    if wrapper is None:
        body = normalized.strip()
        preview = _bound(body)
        return text if preview == body else preview
    header, output = wrapper
    body = output.strip()
    preview = _bound(body)
    if preview == body:
        return text
    return "\n".join([*header, preview])


def has_structured_failure(text: str | None) -> bool:
    """Mirror of the web ``hasStructuredFailure``, computed on the full output."""
    value = (text or "").strip()
    if not value:
        return False
    if value[:12].lower() == "[tool error]":
        return True
    if not value.startswith("{"):
        return False
    try:
        parsed = json.loads(value)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    exit_code = parsed.get("exit_code")
    if exit_code is None:  # the web's `??`: a null exit_code falls through too
        exit_code = parsed.get("exitCode")
    return (
        parsed.get("ok") is False
        or parsed.get("success") is False
        or parsed.get("is_error") is True
        or (isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool) and exit_code != 0)
    )


def truncate_tool_input(value: Any) -> tuple[Any, bool]:
    """Cut long strings and lists inside a tool input; report whether anything was cut."""
    if isinstance(value, str):
        if len(value) > _INPUT_STRING_MAX_CHARS:
            return value[:_INPUT_STRING_MAX_CHARS], True
        return value, False
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        cut = False
        for key, item in value.items():
            out[key], item_cut = truncate_tool_input(item)
            cut = cut or item_cut
        return out, cut
    if isinstance(value, list):
        items = []
        cut = len(value) > _INPUT_LIST_MAX_ITEMS
        for item in value[:_INPUT_LIST_MAX_ITEMS]:
            truncated, item_cut = truncate_tool_input(item)
            items.append(truncated)
            cut = cut or item_cut
        return items, cut
    return value, False


def _is_edit(event: dict[str, Any]) -> bool:
    presentation = event.get("tool_presentation") if isinstance(event.get("tool_presentation"), dict) else {}
    identity = " ".join(str(part) for part in (event.get("tool_name"), presentation.get("tool_name"), presentation.get("label")) if part)
    return bool(_EDIT_IDENTITY.search(re.sub(r"[^a-z0-9]+", " ", identity.lower()).strip()))


def _final_answer_tools() -> frozenset[str]:
    names = _load_rules(str(DEFAULT_RULES_PATH)).get("final_answer_tools") or []
    return frozenset(str(name).lower() for name in names)


def _is_final_answer(event: dict[str, Any]) -> bool:
    """A final-answer tool's input IS the answer the clients render as prose, so it is never cut."""
    presentation = event.get("tool_presentation") if isinstance(event.get("tool_presentation"), dict) else {}
    names = {str(name).lower() for name in (event.get("tool_name"), presentation.get("tool_name")) if name}
    return bool(names & _final_answer_tools())


def _presentation_ref(base: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(base, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def _lite_event(event: dict[str, Any], *, item_timestamp: Any, presentations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    full_input = event.get("tool_input_json")
    # Edits count +/- lines from the whole input; final answers render it as prose.
    is_edit = _is_edit(event) or _is_final_answer(event)
    out: dict[str, Any] = {"id": event["id"], "cursor": event["cursor"], "role": event["role"]}
    for key, default in _EVENT_DEFAULTS.items():
        if key in {"tool_input_json", "tool_output_text", "tool_output_truncated", "tool_output_original_chars"}:
            continue
        value = event.get(key, default)
        if value != default:
            out[key] = value
    if event.get("timestamp") != item_timestamp:
        out["timestamp"] = event.get("timestamp")

    if full_input is not None:
        if is_edit:
            out["tool_input_json"] = full_input
        else:
            out["tool_input_json"], cut = truncate_tool_input(full_input)
            if cut:
                out["tool_input_truncated"] = True

    output = event.get("tool_output_text")
    if output:
        preview = tool_output_preview(output)
        out["tool_output_text"] = preview
        if preview != output:
            out["tool_output_truncated"] = True
            out["tool_output_original_chars"] = len(output)
            if has_structured_failure(output):
                out["tool_output_failed"] = True
    elif event.get("tool_output_truncated"):
        out["tool_output_truncated"] = True
        out["tool_output_original_chars"] = event.get("tool_output_original_chars")

    presentation = event.get("tool_presentation")
    if isinstance(presentation, dict):
        base = {key: value for key, value in presentation.items() if key not in {"tool_input_json", "shell_summary", "children"}}
        ref = _presentation_ref(base)
        presentations.setdefault(ref, base)
        out["tool_presentation_ref"] = ref
        presented_input = presentation.get("tool_input_json")
        if presented_input is not None:
            if presented_input == full_input:
                out["tool_presentation_input"] = "same"
            else:
                # A wrapper's presented input (Codex apply_patch inside exec) is
                # what an edit row counts, so it stays whole for edits too.
                kept, cut = (presented_input, False) if is_edit else truncate_tool_input(presented_input)
                out["tool_presentation_input"] = {"value": kept}
                if cut:
                    # Lets the client fetch the whole presented input on expand.
                    out["tool_input_truncated"] = True
        if presentation.get("shell_summary") is not None:
            out["tool_presentation_shell_summary"] = presentation["shell_summary"]
        if presentation.get("children"):
            children = []
            for child in presentation["children"]:
                if isinstance(child, dict):
                    child_input, child_cut = truncate_tool_input(child.get("tool_input_json"))
                    children.append({**child, "tool_input_json": child_input})
                    if child_cut:
                        out["tool_input_truncated"] = True
                else:
                    children.append(child)
            out["tool_presentation_children"] = children
    return out


def lite_projection(projection: dict[str, Any]) -> dict[str, Any]:
    """Return a lite copy of a full workspace projection."""
    presentations: dict[str, dict[str, Any]] = {}
    focus_session_id = projection.get("focus_session_id")
    items = []
    for item in projection.get("items") or []:
        out: dict[str, Any] = {"kind": item.get("kind"), "timestamp": item.get("timestamp")}
        if item.get("session_id") != focus_session_id:
            out["session_id"] = item.get("session_id")
        for key, default in _ITEM_DEFAULTS.items():
            value = item.get(key, default)
            if value != default:
                out[key] = value
        event = item.get("event")
        if isinstance(event, dict):
            out["event"] = _lite_event(event, item_timestamp=item.get("timestamp"), presentations=presentations)
        items.append(out)
    lite = {key: value for key, value in projection.items() if key != "items"}
    lite["items"] = items
    lite["detail"] = DETAIL_LITE
    lite["tool_presentations"] = presentations
    return lite


def lite_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    """Return a lite copy of a full workspace: lite projection, thread without its session copies."""
    lite = dict(workspace)
    lite["projection"] = lite_projection(workspace["projection"])
    thread = workspace.get("thread")
    if isinstance(thread, dict):
        # The thread lists the same single session the workspace already carries.
        lite["thread"] = {key: value for key, value in thread.items() if key != "sessions"}
    return lite


__all__ = [
    "DETAIL_FULL",
    "DETAIL_LITE",
    "DETAIL_MODES",
    "has_structured_failure",
    "lite_projection",
    "lite_workspace",
    "tool_output_preview",
    "truncate_tool_input",
]
