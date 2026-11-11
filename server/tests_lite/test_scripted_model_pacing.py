"""Paced replies: N tokens at R per second as real protocol deltas, on every protocol."""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests_lite.test_scripted_model_server import PROTOCOLS
from tests_lite.test_scripted_model_server import _message
from tests_lite.test_scripted_model_server import _running_server
from tests_lite.test_scripted_model_server import _text
from tests_lite.test_scripted_model_server import _tool_call
from tests_lite.test_scripted_model_server import _tool_definitions
from zerg.qa import scripted_model_server as server

TOKENS = 20
RATE = 40.0
INITIAL_DELAY = 0.3
PACE = {"tokens": TOKENS, "rate": RATE, "initial_delay": INITIAL_DELAY, "marker_every": 5, "marker_prefix": "PACEUNIT"}
SCRIPT = {
    "turns": [
        {"id": "paced", "match": "PACED_PLAIN", "pace": PACE},
        {"id": "paced-text", "match": "PACED_TEXT", "reply": "one two three four", "pace": {"rate": 40}},
        {
            "id": "paced-tool",
            "match": "PACED_TOOL",
            "pace": PACE,
            "tool_call": {"type": "shell", "name": "shell", "command": "echo hi"},
        },
        {"id": "unpaced", "match": "PLAIN_UNPACED", "reply": "SCRIPT_PLAIN_REPLY"},
    ]
}
EXPECTED = server.paced_text(server.parse_pacing(PACE))


def _path(protocol: str, body: dict) -> str:
    if protocol == "chat":
        body.update(messages=[_message(protocol, body.pop("text"))], stream=True)
        return "/v1/chat/completions"
    if protocol == "responses":
        body.update(input=[_message(protocol, body.pop("text"))], stream=True)
        return "/v1/responses"
    if protocol == "anthropic":
        body.update(
            messages=[_message(protocol, body.pop("text"))], max_tokens=512, stream=True, thinking={"type": "enabled", "budget_tokens": 256}
        )
        return "/v1/messages"
    body["contents"] = [_message(protocol, body.pop("text"))]
    return "/v1beta/models/mock-model:streamGenerateContent"


def _stream(endpoint: str, protocol: str, text: str, *, tools: bool = False) -> tuple[list[tuple[float, dict]], float]:
    """(arrival monotonic seconds, event) for each SSE data line, and the request start."""
    body: dict = {"model": "mock-model", "text": text}
    path = _path(protocol, body)
    if tools:
        body["tools"] = _tool_definitions(protocol)
    parsed = urlsplit(endpoint)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    events: list[tuple[float, dict]] = []
    try:
        connection.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        started = time.monotonic()
        response = connection.getresponse()
        assert response.status == 200
        while line := response.fp.readline():
            now = time.monotonic()
            line = line.decode("utf-8").strip()
            if line.startswith("data: ") and line[6:] != "[DONE]":
                events.append((now, json.loads(line[6:])))
    finally:
        connection.close()
    return events, started


def _delta_arrivals(protocol: str, events: list[tuple[float, dict]], started: float) -> list[tuple[str, float]]:
    out = []
    for arrived, event in events:
        piece = _text(protocol, [event], streaming=True)
        if piece:
            out.append((piece, arrived - started))
    return out


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_paced_reply_streams_one_token_per_delta_on_schedule(tmp_path: Path, protocol: str) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        events, started = _stream(endpoint, protocol, "PACED_PLAIN")
    arrivals = _delta_arrivals(protocol, events, started)
    assert len(arrivals) == TOKENS
    assert "".join(piece for piece, _ in arrivals) == EXPECTED
    for index, (_, seen) in enumerate(arrivals):
        due = INITIAL_DELAY + index / RATE
        # Never early; late only by scheduler and loopback jitter.
        assert due - 0.01 <= seen <= due + 0.25, (index, due, seen)
    # The stream as a whole takes about N / R after the initial delay.
    assert arrivals[-1][1] - arrivals[0][1] == pytest.approx((TOKENS - 1) / RATE, abs=0.15)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_paced_scripted_reply_text_is_split_into_tokens_and_unchanged(tmp_path: Path, protocol: str) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        events, started = _stream(endpoint, protocol, "PACED_TEXT")
    arrivals = _delta_arrivals(protocol, events, started)
    assert [piece for piece, _ in arrivals] == ["one", " two", " three", " four"]


@pytest.mark.parametrize("protocol", ("chat", "responses", "anthropic"))
def test_paced_text_before_a_tool_call_is_paced_and_the_call_still_arrives(tmp_path: Path, protocol: str) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        events, started = _stream(endpoint, protocol, "PACED_TOOL", tools=True)
    arrivals = _delta_arrivals(protocol, events, started)
    assert "".join(piece for piece, _ in arrivals) == EXPECTED
    assert arrivals[-1][1] >= INITIAL_DELAY + (TOKENS - 1) / RATE - 0.01
    name, arguments = _tool_call(protocol, [event for _, event in events], streaming=True)
    assert name
    assert "echo hi" in json.dumps(arguments)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_unpaced_reply_keeps_two_immediate_halves(tmp_path: Path, protocol: str) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        events, started = _stream(endpoint, protocol, "PLAIN_UNPACED")
    arrivals = _delta_arrivals(protocol, events, started)
    # Gemini has always sent the whole reply in one frame; the others send two halves.
    expected = ["SCRIPT_PLAIN_REPLY"] if protocol == "gemini" else ["SCRIPT_PL", "AIN_REPLY"]
    assert [piece for piece, _ in arrivals] == expected
    assert "".join(piece for piece, _ in arrivals) == "SCRIPT_PLAIN_REPLY"
    assert arrivals[-1][1] < 0.25


def test_default_chunking_is_the_historic_two_halves() -> None:
    assert server._text_chunks("abcdef") == [("abc", 0.0), ("def", 0.0)]
    assert server._text_chunks("a") == [("a", 0.0)]


def test_pace_schedule_names_each_marker_and_its_due_time() -> None:
    pace = server.parse_pacing(PACE)
    schedule = server.pace_schedule(pace, server.paced_text(pace))
    assert [(index, word) for index, word, _ in schedule] == [
        (0, "PACEUNIT0000"),
        (5, "PACEUNIT0005"),
        (10, "PACEUNIT0010"),
        (15, "PACEUNIT0015"),
        (19, "PACEUNIT0019"),
    ]
    assert [due for *_, due in schedule] == pytest.approx([0.3, 0.425, 0.55, 0.675, 0.775])


@pytest.mark.parametrize(
    "pace",
    [{}, {"rate": 0}, {"rate": -1}, {"rate": "5"}, {"rate": 5, "bogus": 1}, {"rate": 5, "marker_prefix": "a b"}, {"rate": 5, "tokens": 0}],
)
def test_bad_pacing_is_rejected(tmp_path: Path, pace: dict) -> None:
    script = tmp_path / "bad.json"
    script.write_text(json.dumps({"turns": [{"id": "x", "match": "x", "reply": "y", "pace": pace}]}))
    with pytest.raises(ValueError):
        server.load_script(script)


def test_pace_without_tokens_or_reply_is_rejected(tmp_path: Path) -> None:
    script = tmp_path / "bad.json"
    script.write_text(json.dumps({"turns": [{"id": "x", "match": "x", "pace": {"rate": 5}}]}))
    with pytest.raises(ValueError):
        server.load_script(script)


def test_reasoning_tokens_are_streamed_as_reasoning_content_before_the_reply() -> None:
    pace = server.parse_pacing({"rate": 40, "tokens": 4, "initial_delay": 0.4, "reasoning_tokens": 3})
    chunks = server.reasoning_chunks(pace)
    assert [text for text, _ in chunks] == ["THINKING000 ", "THINKING001 ", "THINKING002 "]
    assert all(0 < at < pace.initial_delay for _, at in chunks)
    assert server.parse_pacing({"rate": 40, "tokens": 4}).reasoning_tokens == 0
    with pytest.raises(ValueError):
        server.reasoning_chunks(server.parse_pacing({"rate": 40, "tokens": 4, "reasoning_tokens": 2}))


def test_chat_stream_carries_reasoning_content_before_the_reply(tmp_path: Path) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": [{"id": "r", "match": "WITH_REASONING", "pace": {**PACE, "reasoning_tokens": 3}}]}))
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        events, _started = _stream(endpoint, "chat", "WITH_REASONING")
    deltas = [event["choices"][0]["delta"] for _, event in events if isinstance(event, dict) and event.get("choices")]
    kinds = [
        "reasoning" if "reasoning_content" in delta else "content" for delta in deltas if "reasoning_content" in delta or "content" in delta
    ]
    assert kinds == ["reasoning"] * 3 + ["content"] * TOKENS


def _reasoning_pieces(protocol: str, events: list[dict]) -> list[str]:
    if protocol == "chat":
        return [c["delta"]["reasoning_content"] for e in events for c in e.get("choices", []) if "reasoning_content" in c.get("delta", {})]
    if protocol == "responses":
        return [e["delta"] for e in events if e.get("type") == "response.reasoning_summary_text.delta"]
    if protocol == "anthropic":
        return [
            e["delta"]["thinking"] for e in events if e.get("type") == "content_block_delta" and e["delta"].get("type") == "thinking_delta"
        ]
    return [
        part["text"]
        for e in events
        for c in e.get("candidates", [])
        for part in c["content"]["parts"]
        if part.get("thought") and "text" in part
    ]


def _shape(events: list[dict]) -> list[str]:
    return [str(e.get("type") or sorted(e)) for e in events]


@pytest.mark.parametrize(
    ("protocol", "tools"),
    [
        ("chat", False),
        ("responses", False),
        ("anthropic", False),
        ("gemini", False),
        ("chat", True),
        ("responses", True),
        ("anthropic", True),
    ],
)
def test_reasoning_is_streamed_before_the_reply_on_every_protocol_and_never_in_reply_text(
    tmp_path: Path, protocol: str, tools: bool
) -> None:
    match = "REASON_TOOL" if tools else "REASON_PLAIN"
    turn = {"id": "r", "match": match, "pace": {**PACE, "reasoning_tokens": 3}}
    if tools:
        turn["tool_call"] = {"type": "shell", "name": "shell", "command": "echo hi"}
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": [turn]}))
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        events, started = _stream(endpoint, protocol, match, tools=tools)
    bodies = [event for _, event in events if isinstance(event, dict)]
    assert _reasoning_pieces(protocol, bodies) == ["THINKING000 ", "THINKING001 ", "THINKING002 "]
    first_reasoning = next(i for i, (_, e) in enumerate(events) if _reasoning_pieces(protocol, [e]))
    reply_events = [e for e in bodies if not _reasoning_pieces(protocol, [e])]  # Gemini marks thoughts as parts of the same shape
    first_text = next(i for i, (_, e) in enumerate(events) if e in reply_events and _text(protocol, [e], streaming=True))
    assert first_reasoning < first_text
    # Reasoning is timed inside the initial delay, so it lands in the stream log with a positive time.
    assert all(0 < arrived - started < INITIAL_DELAY + 0.25 for arrived, e in events if _reasoning_pieces(protocol, [e]))
    assert _text(protocol, reply_events, streaming=True) == EXPECTED
    assert "THINKING" not in _text(protocol, reply_events, streaming=True)
    if protocol == "responses":
        done = [e["response"]["output"] for e in bodies if e.get("type") == "response.completed"][0]
        assert [item["type"] for item in done][:2] == ["reasoning", "message"]
        indexes = {e["item"]["type"]: e["output_index"] for e in bodies if e.get("type") == "response.output_item.done"}
        assert indexes["reasoning"] == 0 and indexes["message"] == 1
        if tools:
            assert indexes["function_call"] == 2
    if protocol == "anthropic":
        starts = [(e["index"], e["content_block"]["type"]) for e in bodies if e.get("type") == "content_block_start"]
        assert starts[:2] == [(0, "thinking"), (1, "text")]
        if tools:
            assert starts[2] == (2, "tool_use")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_zero_reasoning_tokens_leave_every_stream_unchanged(tmp_path: Path, protocol: str) -> None:
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            {
                "turns": [
                    {"id": "a", "match": "ZERO_A", "pace": PACE},
                    {"id": "b", "match": "ZERO_B", "pace": {**PACE, "reasoning_tokens": 0}},
                ]
            }
        )
    )
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt):
        plain, _ = _stream(endpoint, protocol, "ZERO_A")
        zero, _ = _stream(endpoint, protocol, "ZERO_B")
    assert _shape([e for _, e in plain]) == _shape([e for _, e in zero])
    assert not _reasoning_pieces(protocol, [e for _, e in zero])
    assert not any(
        '"output_index": 1' in json.dumps(e) or '"index": 1' in json.dumps(e) for _, e in zero if protocol in {"responses", "anthropic"}
    )
