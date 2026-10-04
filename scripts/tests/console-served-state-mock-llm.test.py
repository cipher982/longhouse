#!/usr/bin/env python3
"""The Console fake LLM releases its marker only after the shell call it issued
really ran: a failed, early, or unrelated tool result must never produce it."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "mock_llm", ROOT / "scripts" / "qa" / "console-served-state-mock-llm.py"
)
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)
mock.TOOL_MIN_SECONDS = 0.3


def prompt(prefix: str = "LH_SERVED_AB") -> str:
    return (
        f'Use the shell tool to run exactly: sleep 6. Then concatenate the prefix "{prefix}" '
        'and suffix "C_D" and reply with only the concatenated result, once, and nothing else.'
    )


PROMPT = prompt()
SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": ["string"]}},
    "required": ["command"],
}


def post(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def chat(base: str, *extra: dict) -> dict:
    messages = [{"role": "user", "content": PROMPT}, *extra]
    tools = [{"type": "function", "function": {"name": "bash", "parameters": SCHEMA}}]
    return post(
        base,
        "/v1/chat/completions",
        {"model": "m", "tools": tools, "messages": messages},
    )["choices"][0]["message"]


def anthropic(base: str, *extra: dict) -> list[dict]:
    messages = [{"role": "user", "content": PROMPT}, *extra]
    tools = [{"name": "Bash", "input_schema": SCHEMA}]
    return post(
        base, "/v1/messages", {"model": "m", "tools": tools, "messages": messages}
    )["content"]


def responses(base: str, *extra: dict) -> list[dict]:
    items = [
        {"role": "user", "content": [{"type": "input_text", "text": PROMPT}]},
        *extra,
    ]
    tools = [
        {
            "type": "function",
            "name": "exec_command",
            "parameters": {"properties": {"cmd": {"type": "string"}}},
        }
    ]
    return post(
        base,
        "/v1/responses",
        {"model": "m", "stream": False, "tools": tools, "input": items},
    )["output"]


def gemini(base: str, text: str, *extra: dict) -> dict:
    contents = [{"role": "user", "parts": [{"text": text}]}, *extra]
    tools = [
        {
            "functionDeclarations": [
                {"name": "run_command", "parametersJsonSchema": SCHEMA}
            ]
        }
    ]
    body = post(
        base, "/v1beta/models/m:generateContent", {"tools": tools, "contents": contents}
    )
    return body["candidates"][0]["content"]["parts"][0]


def failed(reply: str, why: str) -> bool:
    return reply.startswith("LH_TOOL_ROUND_TRIP_FAILED") and why in reply


def main() -> None:
    server = mock.ThreadedServer((mock.HOST, 0), mock.MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://{mock.HOST}:{server.server_port}"
    wait = mock.TOOL_MIN_SECONDS + 0.1
    try:
        # Early: an immediate result is rejected, and stays rejected when replayed.
        call = chat(base)["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"command": "sleep 6"}, call
        early = {"role": "tool", "tool_call_id": call["id"], "content": ""}
        assert failed(chat(base, early)["content"], "before"), "early result accepted"
        time.sleep(wait)
        assert failed(chat(base, early)["content"], "already rejected"), (
            "replayed early result accepted"
        )

        # Unrelated, then a nonzero exit, then success, each on its own call.
        call = chat(base)["tool_calls"][0]
        time.sleep(wait)
        unrelated = {"role": "tool", "tool_call_id": "call_someone_else", "content": ""}
        assert failed(chat(base, unrelated)["content"], "no result"), (
            "unrelated result accepted"
        )
        exit_1 = {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": "Process exited with code 1",
        }
        assert failed(chat(base, exit_1)["content"], "nonzero exit"), (
            "delayed exit 1 accepted (chat)"
        )
        call = chat(base)["tool_calls"][0]
        time.sleep(wait)
        done = {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": "Process exited with code 0",
        }
        assert chat(base, done)["content"] == "LH_SERVED_ABC_D"

        item = responses(base)[0]
        time.sleep(wait)
        output = {
            "type": "function_call_output",
            "call_id": item["call_id"],
            "output": "Exit code: 2",
        }
        reply = responses(base, item, output)[0]["content"][0]["text"]
        assert failed(reply, "nonzero exit"), "delayed exit 2 accepted (responses)"

        use = anthropic(base)[0]
        time.sleep(wait)
        error = {
            "type": "tool_result",
            "tool_use_id": use["id"],
            "is_error": True,
            "content": "denied",
        }
        assert failed(
            anthropic(base, {"role": "user", "content": [error]})[0]["text"], "error"
        )

        # Gemini has no call id: one conversation's call never answers another's.
        gemini(base, prompt("LH_SERVED_ONE"))
        time.sleep(wait)
        response = {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "run_command",
                        "response": {"output": ""},
                    }
                }
            ],
        }
        assert failed(
            gemini(base, prompt("LH_SERVED_TWO"), response)["text"], "no result"
        ), "cross-conversation match"
        assert (
            gemini(base, prompt("LH_SERVED_ONE"), response)["text"]
            == "LH_SERVED_ONEC_D"
        )

        no_tools = post(
            base,
            "/v1/chat/completions",
            {"model": "m", "messages": [{"role": "user", "content": PROMPT}]},
        )
        assert "LH_SERVED_" not in json.dumps(no_tools), no_tools
    finally:
        server.shutdown()
        server.server_close()
    print("console-served-state-mock-llm: ok")


if __name__ == "__main__":
    main()
