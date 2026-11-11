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
        body.update(messages=[_message(protocol, body.pop("text"))], max_tokens=512, stream=True)
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
