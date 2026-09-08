"""Pi native-file evidence shared by producers and the isolated verifier bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from typing import Mapping

from zerg.qa.provider_event_digest import raw_event_digest

# These are the provider-native JSONL shapes used by the Shadow projector. The
# taxonomy is intentionally keyed by native entry/content shape, not by the
# provider-neutral rows emitted below.
PI_SHADOW_TAXONOMY = {
    "session": "state:session_header",
    "message/user/text": "transcript:user",
    "message/user/image+text": "transcript:user_with_image",
    "message/assistant/text": "transcript:assistant",
    "message/assistant/text+thinking+toolCall": "transcript:assistant_tool",
    "message/toolResult": "provider_tool:result",
    "message/toolResult/image": "provider_tool:result_image",
    "model_change": "state:model",
    "thinking_level_change": "state:thinking_level",
    "compaction": "signal:context.compaction",
    "branch_summary": "state:branch",
    "custom": "extension:state",
    "custom_message": "extension:message",
    "session_info": "state:session_info",
}


def _pi_text_content(message: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Return text, tool calls, thinking blocks, and image metadata."""
    texts: list[str] = []
    tools: list[dict[str, Any]] = []
    thinking: list[str] = []
    images: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str):
        return content, tools, thinking, images
    if not isinstance(content, list):
        return "\n".join(texts), tools, thinking, images
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind == "thinking" and block.get("thinking"):
            thinking.append(str(block["thinking"]))
        elif kind == "image":
            images.append({key: block.get(key) for key in ("mimeType", "mime_type") if block.get(key)})
        elif kind in {"toolCall", "tool_call", "tool"}:
            tools.append(dict(block))
    return "\n".join(texts), tools, thinking, images


def _pi_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block["text"]) for block in content if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    )


def _pi_native_shape(entry: Mapping[str, Any]) -> str:
    if entry.get("type") != "message":
        return str(entry.get("type") or "unknown")
    message = entry.get("message")
    if not isinstance(message, Mapping):
        return "message/unknown"
    role = str(message.get("role") or "unknown").strip()
    content = message.get("content")
    block_types: set[str] = set()
    if isinstance(content, list):
        block_types = {str(block.get("type") or "unknown") for block in content if isinstance(block, Mapping)}
    elif isinstance(content, str):
        block_types.add("string")
    suffix = "+".join(sorted(block_types)) or "unknown"
    if role == "toolResult" and "image" in block_types:
        return "message/toolResult/image"
    return f"message/{role}/{suffix}"


def pi_native_shadow_taxonomy(
    rows: list[Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify native Pi evidence without manufacturing provider rows."""
    shapes = {str(key): int(value) for key, value in dict(metadata.get("native_shapes") or {}).items()}
    classes: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for shape, count in shapes.items():
        classification = PI_SHADOW_TAXONOMY.get(shape)
        if classification is None:
            unmapped[shape] = count
        else:
            classes[classification] = classes.get(classification, 0) + count
    calls = {str(row.get("tool_call_id")) for row in rows if row.get("type") == "assistant" and row.get("tool_call_id")}
    results = {str(row.get("tool_call_id")) for row in rows if row.get("type") == "tool_result" and row.get("tool_call_id")}
    return {
        "source": "pi_native_session_jsonl",
        "native_shapes": shapes,
        "shadow_classes": classes,
        "unmapped_shapes": unmapped,
        "tool_call_ids": sorted(calls),
        "tool_result_ids": sorted(results),
        "tool_pairs": sorted(calls & results),
        "tool_calls_without_results": sorted(calls - results),
        "tool_results_without_calls": sorted(results - calls),
        "header_present": bool(metadata.get("has_header")),
        "provider_session_id": metadata.get("provider_session_id"),
    }


def _pi_session_header(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            header = json.loads(stream.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return header if isinstance(header, dict) and header.get("type") == "session" else None


def session_file_for_id(session_dir: Path, session_id: str) -> Path | None:
    """Resolve one exact native session header; never select by mtime."""
    matches = [
        path.resolve()
        for path in session_dir.rglob("*.jsonl")
        if path.is_file() and not path.is_symlink() and (_pi_session_header(path) or {}).get("id") == session_id
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Pi native session id is ambiguous: {session_id}")
    return matches[0] if matches else None


def pi_transcript_rows(transcript: Path) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Parse a Pi session JSONL into Longhouse raw-event rows.

    Returns ``(rows, provider_session_id, metadata)`` where metadata carries the
    first model_change, the jsonl line count, and whether a session header was
    seen (required for a valid transcript binding).
    """
    rows: list[dict[str, Any]] = []
    header_id: str | None = None
    metadata: dict[str, Any] = {
        "lines": 0,
        "model": None,
        "has_header": False,
        "taxonomy": {},
        "cwd": None,
        "version": None,
        "native_shapes": {},
        "provider_session_id": None,
    }
    try:
        lines = transcript.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        return rows, None, {**metadata, "error": f"{type(exc).__name__}: {exc}"}

    source_offset = 0
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace")
        if not line.strip():
            source_offset += len(raw_line)
            continue
        metadata["lines"] += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            source_offset += len(raw_line)
            continue
        if not isinstance(entry, dict):
            source_offset += len(raw_line)
            continue
        kind = str(entry.get("type") or "")
        metadata["taxonomy"][kind] = int(metadata["taxonomy"].get(kind, 0)) + 1
        native_shape = _pi_native_shape(entry)
        metadata["native_shapes"][native_shape] = int(metadata["native_shapes"].get(native_shape, 0)) + 1
        base = {
            "provider_event_type": kind,
            "entry_id": entry.get("id"),
            "parent_id": entry.get("parentId"),
            "timestamp": entry.get("timestamp"),
            "source_offset": source_offset,
            "source_line_sha256": hashlib.sha256(raw_line).hexdigest(),
        }
        if kind == "session":
            header_id = str(entry.get("id") or "") or header_id
            if header_id:
                metadata["has_header"] = True
                metadata["version"] = entry.get("version")
                metadata["cwd"] = entry.get("cwd")
                metadata["provider_session_id"] = header_id
        elif kind == "message":
            message = entry.get("message")
            if not isinstance(message, dict):
                source_offset += len(raw_line)
                continue
            role = str(message.get("role") or "").strip().lower()
            text, tools, thinking, images = _pi_text_content(message)
            row: dict[str, Any] = {
                **base,
                "role": role,
                "text": text,
                "message": {
                    "provider_role": role,
                    "stop_reason": message.get("stopReason"),
                    "error_message": message.get("errorMessage"),
                    "usage": message.get("usage"),
                    "provider": message.get("provider"),
                    "model": message.get("model"),
                },
            }
            if role == "assistant":
                row["native_event_sha256"] = raw_event_digest(entry)
            if role == "user":
                row["type"] = "user"
            elif role == "assistant":
                row["type"] = "assistant"
            elif role == "toolresult":
                row["type"] = "tool_result"
                row["tool_call_id"] = message.get("toolCallId")
                row["tool_name"] = message.get("toolName")
                row["is_error"] = message.get("isError")
                row["text"] = _pi_content_text(message.get("content"))
            else:
                row["type"] = "provider_message"
            if tools:
                row["tool_calls"] = tools
                row["tool_name"] = tools[0].get("name")
                row["tool_call_id"] = tools[0].get("id")
                row["tool_input_json"] = tools[0].get("arguments")
            if thinking:
                row["thinking"] = thinking
            if images:
                row["images"] = images
            if header_id:
                row["provider_session_id"] = header_id
            rows.append(row)
        elif kind == "model_change" and metadata.get("model") is None:
            metadata["model"] = entry.get("modelId")
            rows.append({**base, "type": "model_change", "model": entry.get("modelId"), "provider": entry.get("provider")})
        elif kind in {
            "thinking_level_change",
            "compaction",
            "branch_summary",
            "custom",
            "custom_message",
            "session_info",
            "label",
        }:
            rows.append(
                {
                    **base,
                    "type": kind,
                    "text": entry.get("summary") or entry.get("content") or entry.get("name"),
                    "details": {key: value for key, value in entry.items() if key not in {"type", "id", "parentId", "timestamp"}},
                }
            )
        elif kind:
            rows.append({**base, "type": "provider_event", "details": dict(entry)})
        source_offset += len(raw_line)
    return rows, header_id, metadata


def pi_native_model_evidence(transcript: Path, *, source_canary: str, api_key_configured: bool) -> dict[str, Any] | None:
    """Bind qualification accounting to the final reply and its native history."""
    rows, session_id, _ = pi_transcript_rows(transcript)
    assistants = [row for row in rows if row.get("role") == "assistant"]
    if not session_id or not assistants:
        return None
    last = assistants[-1]
    message = last["message"]
    output_tokens = (message.get("usage") or {}).get("output")
    if (
        message.get("stop_reason") not in {"stop", "length"}
        or message.get("error_message")
        or not str(last.get("text") or "").strip()
        or not message.get("model")
        or type(output_tokens) not in {int, float}
        or output_tokens <= 0
    ):
        return None
    # Pi reports usage per assistant message, not per print invocation. Include
    # tool-call rounds and the earlier turns in this qualification-owned file.
    usage: dict[str, int | float] = {}
    for assistant in assistants:
        for key, value in (assistant["message"].get("usage") or {}).items():
            if type(value) in {int, float}:
                usage[key] = usage.get(key, 0) + value
            elif isinstance(value, Mapping):
                for nested_key, number in value.items():
                    if type(number) in {int, float}:
                        name = f"{key}.{nested_key}"
                        usage[name] = usage.get(name, 0) + number
    event_digest = last["native_event_sha256"]
    model = message["model"]
    return {
        "source_canary": source_canary,
        "operation_evidence": {"live_token_behavior": {"status": "pass", "level": "live_token"}},
        "model": model,
        "auth": {"credential_mode": "api_key", "api_key_source": "env", "api_key_configured": api_key_configured},
        "result_event": {
            "type": "message",
            "provider": message.get("provider"),
            "model": model,
            "model_source": "provider_event",
            "usage": usage,
            "total_cost_usd": usage.get("cost.total"),
            "native_event_sha256": event_digest,
        },
        "source_artifacts": [
            {
                "path": str(transcript.resolve()),
                "sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                "kind": "provider_jsonl_stream",
                "event_type": "message",
                "event_sha256": event_digest,
            }
        ],
    }
