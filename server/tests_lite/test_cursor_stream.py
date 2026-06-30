"""Hermetic tests for the Cursor managed stream-json parser.

Validates ``zerg.services.cursor_stream`` against recorded stream-json event
shapes (system/user/assistant/tool_call/result) with real ``timestamp_ms`` and
per-tool ``startedAtMs``/``completedAtMs``. No network, no DB.
"""

from __future__ import annotations

import os as _os
from datetime import datetime
from datetime import timezone

from cryptography.fernet import Fernet

_os.environ.setdefault("DATABASE_URL", "sqlite://")
_os.environ.setdefault("TESTING", "1")
_os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
_os.environ.setdefault("JWT_SECRET", "test-jwt-secret-long-enough")
_os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-long-enough")
_os.environ.setdefault("AUTH_DISABLED", "1")

from zerg.services.cursor_stream import CursorStreamBuilder
from zerg.services.cursor_stream import parse_stream_json

SESSION_ID = "2dc300a4-093a-4574-8ad4-c21aff322323"
CALL_ID = "call_QWnNRTgrW1Kh2FSH3hYOjgr3"


def _system_init(ts_ms: int | None = None) -> dict:
    o = {
        "type": "system",
        "subtype": "init",
        "apiKeySource": "login",
        "cwd": "/tmp/cursor-fixtures",
        "session_id": SESSION_ID,
        "model": "GPT-5.2 Medium",
        "permissionMode": "default",
    }
    if ts_ms is not None:
        o["timestamp_ms"] = ts_ms
    return o


def _user(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "session_id": SESSION_ID,
    }


def _assistant(text: str, ts_ms: int) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "session_id": SESSION_ID,
        "model_call_id": "mc-1",
        "timestamp_ms": ts_ms,
    }


def _tool_call_started(ts_ms: int, started_ms: int) -> dict:
    return {
        "type": "tool_call",
        "subtype": "started",
        "call_id": f"{CALL_ID}\nfc_internal_1",
        "tool_call": {
            "shellToolCall": {
                "args": {
                    "command": "sleep 1 && echo DONE",
                    "toolCallId": CALL_ID,
                    "workingDirectory": "",
                    "timeout": 10000,
                },
                "result": None,
                "description": "run a command",
            },
            "toolCallId": CALL_ID,
            "startedAtMs": str(started_ms),
            "completedAtMs": None,
            "hookAdditionalContexts": [],
        },
        "model_call_id": "mc-1",
        "session_id": SESSION_ID,
        "timestamp_ms": ts_ms,
    }


def _tool_call_completed(ts_ms: int, started_ms: int, completed_ms: int) -> dict:
    return {
        "type": "tool_call",
        "subtype": "completed",
        "call_id": f"{CALL_ID}\nfc_internal_1",
        "tool_call": {
            "shellToolCall": {
                "args": {
                    "command": "sleep 1 && echo DONE",
                    "toolCallId": CALL_ID,
                    "workingDirectory": "",
                    "timeout": 10000,
                },
                "result": {
                    "success": {
                        "command": "sleep 1 && echo DONE",
                        "exitCode": 0,
                        "stdout": "DONE\n",
                        "stderr": "",
                        "executionTime": 1010,
                        "localExecutionTimeMs": 1001,
                    }
                },
                "description": "run a command",
            },
            "toolCallId": CALL_ID,
            "startedAtMs": str(started_ms),
            "completedAtMs": str(completed_ms),
            "hookAdditionalContexts": [],
        },
        "model_call_id": "mc-1",
        "session_id": SESSION_ID,
        "timestamp_ms": ts_ms,
    }


def _result(duration_ms: int) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": duration_ms,
        "duration_api_ms": duration_ms,
        "is_error": False,
        "result": "DONE",
        "session_id": SESSION_ID,
        "request_id": "req-1",
        "usage": {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 0, "cacheWriteTokens": 0},
    }


def _full_stream() -> str:
    lines = [
        _system_init(),
        _user("run the command"),
        _assistant("I will run it.", 1782839369336),
        _tool_call_started(1782839369336, 1782839369400),
        _tool_call_completed(1782839373944, 1782839369400, 1782839373900),
        _assistant("COMPLETE", 1782839374000),
        _result(12835),
    ]
    return "\n".join(json_dumps(o) for o in lines) + "\n"


def json_dumps(o: dict) -> str:
    import json

    return json.dumps(o, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parse_full_stream_real_timestamps():
    session, diag = parse_stream_json(_full_stream())

    assert diag.session_id == SESSION_ID
    assert diag.cwd == "/tmp/cursor-fixtures"
    assert diag.model == "GPT-5.2 Medium"
    assert diag.timestamp_fidelity == "real"
    assert diag.event_count == len(session.events)
    # session is managed-local
    assert session.execution_home == "managed_local"
    assert session.provider == "cursor"
    assert session.provider_session_id == SESSION_ID
    assert session.cwd == "/tmp/cursor-fixtures"
    assert session.project == "cursor-fixtures"


def test_assistant_and_tool_events_use_real_timestamps():
    session, _ = parse_stream_json(_full_stream())
    by_role = [(e.role, e.content_text, e.tool_name, e.tool_call_id) for e in session.events]

    # system, user, assistant(text), assistant(tool call), tool(result), assistant(text)
    assert by_role[0][0] == "system"
    assert by_role[1][0] == "user"
    assert by_role[1][1] == "run the command"
    assert by_role[2][0] == "assistant"
    assert by_role[2][1] == "I will run it."
    # tool call (started) -> assistant tool event
    assert by_role[3][0] == "assistant"
    assert by_role[3][2] == "shell"  # shellToolCall -> "shell"
    assert by_role[3][3] == CALL_ID
    # tool result (completed) -> tool event
    assert by_role[4][0] == "tool"
    assert by_role[4][2] == "shell"
    assert by_role[4][3] == CALL_ID
    tool_result_event = next(e for e in session.events if e.role == "tool" and e.tool_name == "shell")
    assert "DONE" in (tool_result_event.tool_output_text or "")  # carries stdout
    # final assistant
    assert by_role[5][0] == "assistant"
    assert by_role[5][1] == "COMPLETE"

    # Real timestamps: the assistant text at 1782839369336
    ts_map = {
        "I will run it.": 1782839369336,
        "COMPLETE": 1782839374000,
    }
    for e in session.events:
        if e.content_text in ts_map:
            expected = datetime.fromtimestamp(ts_map[e.content_text] / 1000.0, tz=timezone.utc)
            assert e.timestamp == expected, (e.content_text, e.timestamp, expected)


def test_tool_call_started_uses_startedatms_completed_uses_completedatms():
    session, _ = parse_stream_json(_full_stream())
    tool_call_event = next(e for e in session.events if e.role == "assistant" and e.tool_name == "shell")
    tool_result_event = next(e for e in session.events if e.role == "tool" and e.tool_name == "shell")
    # started -> 1782839369400 (startedAtMs), not the top-level timestamp_ms 1782839369336
    assert tool_call_event.timestamp == datetime.fromtimestamp(1782839369400 / 1000.0, tz=timezone.utc)
    # completed -> 1782839373900 (completedAtMs)
    assert tool_result_event.timestamp == datetime.fromtimestamp(1782839373900 / 1000.0, tz=timezone.utc)


def test_tool_input_strips_linkage_keys():
    session, _ = parse_stream_json(_full_stream())
    tool_call_event = next(e for e in session.events if e.role == "assistant" and e.tool_name == "shell")
    assert tool_call_event.tool_input_json is not None
    assert "command" in tool_call_event.tool_input_json
    assert "toolCallId" not in tool_call_event.tool_input_json  # linkage stripped
    assert "conversationId" not in tool_call_event.tool_input_json


def test_monotonic_carry_forward_for_unstamped_events():
    session, _ = parse_stream_json(_full_stream())
    # user event has no timestamp_ms; it carries forward from the prior real ts.
    # The first real ts is the assistant at 1782839369336, but user comes before
    # it, so it falls back to wall-clock now (no prior real ts yet). It must
    # still be <= the assistant timestamp and monotonic.
    user_ev = next(e for e in session.events if e.role == "user")
    asst_ev = next(e for e in session.events if e.content_text == "I will run it.")
    assert user_ev.timestamp <= asst_ev.timestamp


def test_result_extends_ended_at_by_duration():
    session, diag = parse_stream_json(_full_stream())
    # Last real ts before result is the final assistant at 1782839374000.
    # result duration_ms=12835 -> ended_at = that + 12835ms.
    expected_end_ms = 1782839374000 + 12835
    assert diag.ended_at_ms == expected_end_ms
    assert session.ended_at is not None
    assert int(session.ended_at.timestamp() * 1000) == expected_end_ms


def test_unknown_event_types_counted_not_crashed():
    stream = (
        json_dumps(_system_init())
        + "\n"
        + json_dumps({"type": "weird_new_event", "payload": {"x": 1}})
        + "\n"
        + json_dumps(_assistant("hi", 1782839369336))
        + "\n"
    )
    session, diag = parse_stream_json(stream)
    assert diag.unknown_event_types.get("weird_new_event") == 1
    # system + assistant still decoded
    assert any(e.role == "assistant" and e.content_text == "hi" for e in session.events)


def test_malformed_lines_skipped():
    stream = "not json\n" + json_dumps(_assistant("hi", 1782839369336)) + "\n\n"
    session, diag = parse_stream_json(stream)
    assert any(e.role == "assistant" for e in session.events)
    assert diag.event_count >= 1


def test_builder_feed_line_streaming_matches_batch():
    batch_session, _ = parse_stream_json(_full_stream())
    builder = CursorStreamBuilder()
    for line in _full_stream().splitlines():
        builder.feed_line(line)
    live_session = builder.build()
    assert len(live_session.events) == len(batch_session.events)
    assert [e.role for e in live_session.events] == [e.role for e in batch_session.events]


def test_empty_stream_produces_minimal_session():
    session, diag = parse_stream_json("")
    assert session.events == []
    assert diag.session_id is None
    assert session.started_at is not None
    assert session.ended_at is not None
