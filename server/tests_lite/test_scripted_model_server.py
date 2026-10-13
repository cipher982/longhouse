"""The scripted model server's scenario and wire-protocol contract."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from zerg.qa import scripted_model_server

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path(__file__).parent / "fixtures" / "scripted_model_scenarios.json"
PROTOCOLS = ("chat", "responses", "anthropic", "gemini")
TURN_KINDS = ("plain", "tool", "steer", "resume")
SHELL_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
}


@contextmanager
def _running_server(tmp_path: Path, *, script_from_env: bool = False, script_path: Path | None = None):
    port_file = tmp_path / "server.port"
    receipt_file = tmp_path / "receipt.json"
    scenario_path = script_path or SCENARIOS
    command = [
        sys.executable,
        "-m",
        "zerg.qa.scripted_model_server",
        "--port-file",
        str(port_file),
        "--receipt-file",
        str(receipt_file),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SERVER_ROOT)
    if script_from_env:
        environment["LONGHOUSE_SCRIPTED_MODEL_SCRIPT"] = str(scenario_path)
    else:
        command.extend(("--script", str(scenario_path)))
    process = subprocess.Popen(
        command,
        cwd=SERVER_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not port_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                output, _ = process.communicate()
                raise AssertionError(f"scripted server exited before binding: {output}")
            time.sleep(0.01)
        if not port_file.exists():
            raise AssertionError("scripted server did not publish its bound port")
        yield f"http://127.0.0.1:{port_file.read_text(encoding='ascii').strip()}", receipt_file
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def _http(endpoint: str, path: str, payload: dict | None = None):
    parsed = urlsplit(endpoint)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        connection.request("POST" if body is not None else "GET", path, body, headers)
        response = connection.getresponse()
        raw = response.read()
        assert response.status == 200, raw.decode("utf-8", errors="replace")
        return response.getheader("Content-Type", ""), raw
    finally:
        connection.close()


def _response_values(content_type: str, raw: bytes) -> list[dict]:
    if "text/event-stream" not in content_type:
        return [json.loads(raw)]
    values = []
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("data: ") or line[6:] == "[DONE]":
            continue
        values.append(json.loads(line[6:]))
    return values


def _message(protocol: str, text: str) -> dict:
    if protocol in ("chat", "anthropic"):
        return {"role": "user", "content": text}
    if protocol == "responses":
        return {"role": "user", "content": [{"type": "input_text", "text": text}]}
    return {"role": "user", "parts": [{"text": text}]}


def _assistant_message(protocol: str, text: str) -> dict:
    if protocol == "chat":
        return {"role": "assistant", "content": text}
    if protocol == "responses":
        return {"role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if protocol == "anthropic":
        return {"role": "assistant", "content": [{"type": "text", "text": text}]}
    return {"role": "model", "parts": [{"text": text}]}


def _tool_definitions(protocol: str) -> list[dict]:
    if protocol == "chat":
        return [
            {
                "type": "function",
                "function": {"name": "bash", "parameters": SHELL_SCHEMA},
            }
        ]
    if protocol == "responses":
        return [
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            }
        ]
    if protocol == "anthropic":
        return [{"name": "Bash", "input_schema": SHELL_SCHEMA}]
    return [{"functionDeclarations": [{"name": "run_command", "parametersJsonSchema": SHELL_SCHEMA}]}]


def _request(
    endpoint: str,
    protocol: str,
    messages: list[dict],
    *,
    streaming: bool,
    tools: bool = False,
):
    body = {"model": "mock-model"}
    if protocol == "chat":
        path = "/v1/chat/completions"
        body.update(messages=messages, stream=streaming)
        if tools:
            body["tools"] = _tool_definitions(protocol)
    elif protocol == "responses":
        path = "/v1/responses"
        body.update(input=messages, stream=streaming)
        if tools:
            body["tools"] = _tool_definitions(protocol)
    elif protocol == "anthropic":
        path = "/v1/messages"
        body.update(messages=messages, max_tokens=512, stream=streaming)
        if tools:
            body["tools"] = _tool_definitions(protocol)
    else:
        method = "streamGenerateContent" if streaming else "generateContent"
        path = f"/v1beta/models/mock-model:{method}"
        body["contents"] = messages
        if tools:
            body["tools"] = _tool_definitions(protocol)
    content_type, raw = _http(endpoint, path, body)
    return _response_values(content_type, raw)


def _text(protocol: str, values: list[dict], *, streaming: bool) -> str:
    if protocol == "chat":
        if streaming:
            return "".join(choice.get("delta", {}).get("content", "") for item in values for choice in item.get("choices", []))
        return values[0]["choices"][0]["message"].get("content") or ""
    if protocol == "responses":
        if streaming:
            return "".join(item.get("delta", "") for item in values if item.get("type") == "response.output_text.delta")
        return "".join(
            part.get("text", "")
            for item in values[0].get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
    if protocol == "anthropic":
        if streaming:
            return "".join(
                item.get("delta", {}).get("text", "")
                for item in values
                if item.get("type") == "content_block_delta" and item.get("delta", {}).get("type") == "text_delta"
            )
        return "".join(item.get("text", "") for item in values[0].get("content", []) if item.get("type") == "text")
    return "".join(
        part.get("text", "")
        for item in values
        for candidate in item.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    )


def _tool_call(protocol: str, values: list[dict], *, streaming: bool) -> tuple[str, dict]:
    if protocol == "chat":
        if streaming:
            call = next(
                call for item in values for choice in item.get("choices", []) for call in choice.get("delta", {}).get("tool_calls", [])
            )
        else:
            call = values[0]["choices"][0]["message"]["tool_calls"][0]
        return call["function"]["name"], json.loads(call["function"]["arguments"])
    if protocol == "responses":
        if streaming:
            call = next(
                item["item"]
                for item in values
                if item.get("type") == "response.output_item.done" and item.get("item", {}).get("type") == "function_call"
            )
        else:
            call = next(item for item in values[0]["output"] if item.get("type") == "function_call")
        return call["name"], json.loads(call["arguments"])
    if protocol == "anthropic":
        if not streaming:
            call = next(item for item in values[0]["content"] if item["type"] == "tool_use")
            return call["name"], call["input"]
        name = None
        arguments = ""
        for item in values:
            if item.get("type") == "content_block_start":
                block = item.get("content_block", {})
                if block.get("type") == "tool_use":
                    name = block["name"]
            elif item.get("type") == "content_block_delta":
                delta = item.get("delta", {})
                if delta.get("type") == "input_json_delta":
                    arguments += delta.get("partial_json", "")
        assert name is not None
        return name, json.loads(arguments)
    call = next(
        part["functionCall"]
        for item in values
        for candidate in item.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
        if "functionCall" in part
    )
    return call["name"], call["args"]


def _tool_result_messages(protocol: str, name: str, arguments: dict) -> list[dict]:
    if protocol == "chat":
        call_id = "call_test"
        return [
            _message(protocol, "SCRIPT_TOOL_START"),
            {
                "role": "assistant",
                "content": "SCRIPT_TOOL_REQUESTED",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "TOOL_RESULT_MARKER"},
        ]
    if protocol == "responses":
        call_id = "call_test"
        return [
            _message(protocol, "SCRIPT_TOOL_START"),
            _assistant_message(protocol, "SCRIPT_TOOL_REQUESTED"),
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            },
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": "TOOL_RESULT_MARKER",
            },
        ]
    if protocol == "anthropic":
        tool_id = "toolu_test"
        return [
            _message(protocol, "SCRIPT_TOOL_START"),
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "SCRIPT_TOOL_REQUESTED"},
                    {"type": "tool_use", "id": tool_id, "name": name, "input": arguments},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "TOOL_RESULT_MARKER",
                    }
                ],
            },
        ]
    return [
        _message(protocol, "SCRIPT_TOOL_START"),
        {
            "role": "model",
            "parts": [
                {"text": "SCRIPT_TOOL_REQUESTED"},
                {"functionCall": {"name": name, "args": arguments}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": name,
                        "response": {"output": "TOOL_RESULT_MARKER"},
                    }
                }
            ],
        },
    ]


def _receipt(endpoint: str) -> dict:
    content_type, raw = _http(endpoint, "/_scripted_model/receipt")
    assert "application/json" in content_type
    return json.loads(raw)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("turn_kind", TURN_KINDS)
@pytest.mark.parametrize("streaming", (False, True))
def test_real_http_protocols_serve_each_scripted_turn(tmp_path: Path, protocol: str, turn_kind: str, streaming: bool):
    with _running_server(tmp_path) as (endpoint, receipt_file):
        expected_turn_ids: list[str]
        if turn_kind == "plain":
            values = _request(
                endpoint,
                protocol,
                [_message(protocol, "SCRIPT_PLAIN")],
                streaming=streaming,
            )
            assert _text(protocol, values, streaming=streaming) == "SCRIPT_PLAIN_REPLY"
            expected_turn_ids = ["plain-reply"]
        elif turn_kind == "tool":
            values = _request(
                endpoint,
                protocol,
                [_message(protocol, "SCRIPT_TOOL_START")],
                streaming=streaming,
                tools=True,
            )
            assert _text(protocol, values, streaming=streaming) == "SCRIPT_TOOL_REQUESTED"
            name, arguments = _tool_call(protocol, values, streaming=streaming)
            assert name
            command = arguments.get("command", arguments.get("cmd"))
            assert command == "sleep 30 && echo TOOL_RESULT_MARKER"
            values = _request(
                endpoint,
                protocol,
                _tool_result_messages(protocol, name, arguments),
                streaming=streaming,
                tools=True,
            )
            assert _text(protocol, values, streaming=streaming) == "SCRIPT_TOOL_FINAL"
            expected_turn_ids = ["tool-start", "tool-result-reply"]
        elif turn_kind == "steer":
            values = _request(
                endpoint,
                protocol,
                [_message(protocol, "SCRIPT_STEER_INITIAL")],
                streaming=streaming,
            )
            assert _text(protocol, values, streaming=streaming) == "SCRIPT_BEFORE_STEER"
            messages = [
                _message(protocol, "SCRIPT_STEER_INITIAL"),
                _assistant_message(protocol, "SCRIPT_BEFORE_STEER"),
                _message(protocol, "SCRIPT_STEERED"),
            ]
            values = _request(endpoint, protocol, messages, streaming=streaming)
            assert _text(protocol, values, streaming=streaming) == "SCRIPT_AFTER_STEER"
            expected_turn_ids = ["steer-initial", "steer-result"]
        else:
            values = _request(
                endpoint,
                protocol,
                [_message(protocol, "SCRIPT_RESUME_PLANT")],
                streaming=streaming,
            )
            planted = _text(protocol, values, streaming=streaming)
            assert planted == "PLANTED_VALUE=blue-42"
            messages = [
                _message(protocol, "SCRIPT_RESUME_PLANT"),
                _assistant_message(protocol, planted),
                _message(protocol, "SCRIPT_RESUME_NOW"),
            ]
            values = _request(endpoint, protocol, messages, streaming=streaming)
            assert _text(protocol, values, streaming=streaming) == "blue-42"
            expected_turn_ids = ["resume-plant", "resume-echo"]

        receipt = _receipt(endpoint)
        scripted = receipt["scripted_model"]
        assert scripted == {
            "endpoint": endpoint,
            "script_sha256": hashlib.sha256(SCENARIOS.read_bytes()).hexdigest(),
            "served_requests": len(expected_turn_ids),
        }
        assert receipt["requests"] == [
            {"request_id": index, "matched_turn_id": turn_id, "status": "served"}
            for index, turn_id in enumerate(expected_turn_ids, start=1)
        ]
        assert json.loads(receipt_file.read_text(encoding="utf-8")) == receipt


def test_scripted_reply_template_uses_the_current_pi_input_marker(tmp_path: Path) -> None:
    script = tmp_path / "pi-marker.json"
    script.write_text(
        json.dumps(
            {
                "turns": [
                    {
                        "id": "pi-input-marker",
                        "match": "LONGHOUSE_PI_PROBE_",
                        "reply": "{{input_marker}}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    marker = f"LONGHOUSE_PI_PROBE_{'a' * 32}"
    prompt = f"Reply with exactly {marker} and nothing else."
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt_file):
        values = _request(endpoint, "chat", [_message("chat", prompt)], streaming=False)

        assert _text("chat", values, streaming=False) == marker
        assert _receipt(endpoint)["requests"] == [{"request_id": 1, "matched_turn_id": "pi-input-marker", "status": "served"}]


def test_scripted_responses_tool_only_continuation_uses_fixture_and_counts_only_success(tmp_path: Path) -> None:
    script = tmp_path / "responses-continuation.json"
    script.write_text(
        json.dumps(
            {
                "turns": [
                    {
                        "id": "initial-tool",
                        "match": "SCRIPT_TOOL_INITIAL",
                        "tool_call": {"type": "shell", "name": "exec_command", "command": "echo initial"},
                    },
                    {
                        "id": "tool-result-continuation",
                        "match": "SCRIPT_TOOL_RESULT_ONLY",
                        "tool_call": {"type": "shell", "name": "exec_command", "command": "echo continued"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    with _running_server(tmp_path, script_path=script) as (endpoint, _receipt_file):
        initial = _request(
            endpoint,
            "responses",
            [_message("responses", "SCRIPT_TOOL_INITIAL")],
            streaming=False,
            tools=True,
        )
        initial_call = next(item for item in initial[0]["output"] if item["type"] == "function_call")
        continuation = _request(
            endpoint,
            "responses",
            [
                {
                    "type": "function_call_output",
                    "call_id": initial_call["call_id"],
                    "output": "SCRIPT_TOOL_RESULT_ONLY",
                }
            ],
            streaming=False,
            tools=True,
        )
        continuation_call = next(item for item in continuation[0]["output"] if item["type"] == "function_call")
        assert continuation_call["name"] == "exec_command"
        assert json.loads(continuation_call["arguments"]) == {"cmd": "echo continued"}

        parsed = urlsplit(endpoint)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        failed_payload = {
            "model": "mock-model",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": continuation_call["call_id"],
                    "output": "UNMATCHED_TOOL_RESULT",
                }
            ],
            "tools": _tool_definitions("responses"),
            "stream": False,
        }
        try:
            connection.request(
                "POST",
                "/v1/responses",
                json.dumps(failed_payload),
                {"Content-Type": "application/json"},
            )
            failed_response = connection.getresponse()
            failed_response.read()
            assert failed_response.status == 400
        finally:
            connection.close()

        receipt = _receipt(endpoint)
        assert receipt["scripted_model"]["served_requests"] == 2
        assert receipt["requests"] == [
            {"request_id": 1, "matched_turn_id": "initial-tool", "status": "served"},
            {"request_id": 2, "matched_turn_id": "tool-result-continuation", "status": "served"},
        ]


def test_scripted_tool_emission_skips_legacy_user_text_planner(monkeypatch) -> None:
    class FakeServer:
        def __init__(self) -> None:
            self.turn_ids = []
            self.statuses = {}

        def begin_model_request(self, turn_id: str | None) -> int:
            request_id = len(self.turn_ids) + 1
            self.turn_ids.append(turn_id)
            self.statuses[request_id] = "pending"
            return request_id

        def mark_model_request_served(self, request_id: int) -> None:
            self.statuses[request_id] = "served"

        def mark_model_request_failed(self, request_id: int) -> None:
            self.statuses[request_id] = "failed"

    handler = scripted_model_server.MockHandler.__new__(scripted_model_server.MockHandler)
    handler.server = FakeServer()
    sent_responses = []

    def send_json(status, payload, *, model_request_id=None):
        if model_request_id is not None:
            handler.server.mark_model_request_served(model_request_id)
        sent_responses.append((status, payload))

    handler._send_json = send_json
    monkeypatch.setattr(
        scripted_model_server,
        "_reply_text",
        lambda _payload: pytest.fail("scripted tool emission used the legacy user-text planner"),
    )

    handler._responses_tool(
        {"model": "mock-model", "stream": False, "messages": [{"role": "user", "content": "fixture prompt"}]},
        "exec_command",
        {"cmd": "echo scripted"},
        turn_id="scripted-turn",
    )

    assert sent_responses[0][0] == 200
    assert handler.server.turn_ids == ["scripted-turn"]
    assert handler.server.statuses == {1: "served"}


def test_script_path_can_come_from_environment(tmp_path: Path):
    with _running_server(tmp_path, script_from_env=True) as (endpoint, _receipt_file):
        values = _request(
            endpoint,
            "chat",
            [_message("chat", "SCRIPT_PLAIN")],
            streaming=False,
        )
        assert _text("chat", values, streaming=False) == "SCRIPT_PLAIN_REPLY"
        assert _receipt(endpoint)["scripted_model"]["served_requests"] == 1


def test_unmatched_script_turn_uses_the_legacy_default(tmp_path: Path):
    with _running_server(tmp_path) as (endpoint, _receipt_file):
        values = _request(
            endpoint,
            "chat",
            [_message("chat", "UNMATCHED_DEFAULT_REPLY")],
            streaming=False,
        )
        assert _text("chat", values, streaming=False) == "UNMATCHED_DEFAULT_REPLY"
        receipt = _receipt(endpoint)
        assert receipt["scripted_model"]["served_requests"] == 1
        assert receipt["requests"] == [{"request_id": 1, "matched_turn_id": None, "status": "served"}]


def test_failed_response_write_marks_the_receipt_failed() -> None:
    server = scripted_model_server.ThreadedServer(
        ("127.0.0.1", 0),
        scripted_model_server.MockHandler,
        script_turns=[],
        script_sha256="a" * 64,
    )
    request_id = server.begin_model_request("fixture-turn")
    handler = scripted_model_server.MockHandler.__new__(scripted_model_server.MockHandler)
    handler.server = server
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda *_args, **_kwargs: None

    class BrokenPipeWriter:
        def write(self, _body: bytes) -> None:
            assert server.receipt()["requests"][0]["status"] == "served"
            raise BrokenPipeError("client disconnected before body")

    handler.wfile = BrokenPipeWriter()
    try:
        assert server.receipt()["requests"][0]["status"] == "pending"
        with pytest.raises(BrokenPipeError, match="client disconnected"):
            handler._send_json(200, {"ok": True}, model_request_id=request_id)
        receipt = server.receipt()
        assert receipt["scripted_model"]["served_requests"] == 0
        assert receipt["requests"] == [{"request_id": request_id, "matched_turn_id": "fixture-turn", "status": "failed"}]
    finally:
        server.server_close()


def test_server_module_is_importable():
    assert callable(scripted_model_server.main)
