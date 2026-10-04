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
spec = importlib.util.spec_from_file_location("mock_llm", ROOT / "scripts" / "qa" / "console-served-state-mock-llm.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)
mock.TOOL_MIN_SECONDS = 0.3

PROMPT = (
    'Use the shell tool to run exactly: sleep 6. Then concatenate the prefix "LH_SERVED_AB" '
    'and suffix "C_D" and reply with only the concatenated result, once, and nothing else.'
)
SCHEMA = {"type": "object", "properties": {"command": {"type": ["string"]}}, "required": ["command"]}


def post(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def chat(base: str, *extra: dict) -> dict:
    messages = [{"role": "user", "content": PROMPT}, *extra]
    tools = [{"type": "function", "function": {"name": "bash", "parameters": SCHEMA}}]
    return post(base, "/v1/chat/completions", {"model": "m", "tools": tools, "messages": messages})["choices"][0]["message"]


def anthropic(base: str, *extra: dict) -> list[dict]:
    messages = [{"role": "user", "content": PROMPT}, *extra]
    tools = [{"name": "Bash", "input_schema": SCHEMA}]
    return post(base, "/v1/messages", {"model": "m", "tools": tools, "messages": messages})["content"]


def main() -> None:
    server = mock.ThreadedServer((mock.HOST, 0), mock.MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://{mock.HOST}:{server.server_port}"
    try:
        call = chat(base)["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"command": "sleep 6"}, call
        result = {"role": "tool", "tool_call_id": call["id"], "content": ""}

        early = chat(base, result)["content"]
        assert early.startswith("LH_TOOL_ROUND_TRIP_FAILED") and "before" in early, early

        time.sleep(0.4)
        unrelated = chat(base, {**result, "tool_call_id": "call_someone_else"})["content"]
        assert unrelated.startswith("LH_TOOL_ROUND_TRIP_FAILED"), unrelated
        assert chat(base, result)["content"] == "LH_SERVED_ABC_D"

        use = anthropic(base)[0]
        time.sleep(0.4)
        error = {"type": "tool_result", "tool_use_id": use["id"], "is_error": True, "content": "exit 1"}
        failed = anthropic(base, {"role": "user", "content": [error]})[0]["text"]
        assert failed.startswith("LH_TOOL_ROUND_TRIP_FAILED") and "error" in failed, failed

        no_tools = post(base, "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": PROMPT}]})
        assert "LH_SERVED_" not in json.dumps(no_tools), no_tools
    finally:
        server.shutdown()
        server.server_close()
    print("console-served-state-mock-llm: ok")


if __name__ == "__main__":
    main()
