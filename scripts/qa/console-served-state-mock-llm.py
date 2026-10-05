#!/usr/bin/env python3
"""Loopback-only fake LLM endpoint for the Console served-state CI proof.

This serves the upstream wire protocols used by the stock CLI fixtures. The
served-state marker is reconstructed from the request, never embedded here.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

HOST = "127.0.0.1"
INPUT_TOKENS = 32
OUTPUT_TOKENS = 8
TOTAL_TOKENS = INPUT_TOKENS + OUTPUT_TOKENS
PROMPT_MARKER = re.compile(
    r'concatenate the prefix "([^"]+)" and suffix "([^"]+)"', re.IGNORECASE
)
GEMINI_GENERATE = re.compile(
    r"(?:^|/)models/(.+):(streamGenerateContent|generateContent|countTokens)$"
)


def _string_leaves(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_leaves(item)


def _user_text(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("role") == "user":
            found.extend(_string_leaves(value.get("content", value.get("parts"))))
        for item in value.values():
            found.extend(_user_text(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_user_text(item))
    return found


def _reply_text(payload: dict) -> str:
    request_text = "\n".join(_string_leaves(payload))
    match = PROMPT_MARKER.search(request_text)
    if match:
        reply = match.group(1) + match.group(2)
        if reply.startswith("LH_SERVED_") and re.fullmatch(r"[A-Z0-9_]+", reply):
            return reply
        raise ValueError("served-state marker has an unexpected shape")

    user_text = "\n".join(_user_text(payload)).strip()
    if not user_text and isinstance(payload.get("input"), str):
        user_text = payload["input"].strip()
    if not user_text:
        raise ValueError("generation request contains no user text")
    # Startup probes that send a normal prompt receive request-derived text. The
    # e2e's marker prompt always takes the exact-match branch above.
    return user_text


# The e2e prompt asks for exactly this command through the CLI's own shell tool;
# the marker is answered only once that tool's result comes back, so the turn
# really runs a tool for six seconds before it replies.
TOOL_COMMAND = "sleep 6"
SHELL_TOOL_NAMES = (
    "bash",
    "shell",
    "exec_command",
    "shell_command",
    "run_shell_command",
    "run_command",
)
TOOL_RESULT_TYPES = {
    "tool_result",
    "function_call_output",
    "custom_tool_call_output",
    "local_shell_call_output",
}
# Each prompt (keyed by its marker) is judged on the latest shell call issued
# for it. That call's first result decides its outcome for good: a result
# arriving sooner than the command can run, or reporting a failure, means the
# call failed, was refused, or was backgrounded, and replaying it later never
# turns it into a success. A fresh call starts a fresh outcome, so older
# rejected calls left in the history do not block it.
TOOL_MIN_SECONDS = 6.0
# `sleep 6` prints nothing; a shell result naming a nonzero exit is a failure.
NONZERO_EXIT = re.compile(
    r"\bexit(?:ed)?(?:\s+with)?(?:\s+(?:code|status))?\s*[:=]?\s*(-?[1-9]\d*)\b",
    re.IGNORECASE,
)
EXIT_FIELDS = {
    "exit_code",
    "exitcode",
    "exit_status",
    "exitstatus",
    "returncode",
    "return_code",
}
FAILED_STATUSES = {"error", "failed", "failure"}
_latest: dict[str, tuple[str, float]] = {}  # marker -> (call key, issued at)
_outcomes: dict[
    str, str | None
] = {}  # call key -> failure reason, None when it succeeded
_lock = threading.Lock()


def _issue(marker: str, key: str) -> None:
    with _lock:
        _latest[marker] = (key, time.monotonic())


def _structured_failure(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if (
                lowered in EXIT_FIELDS
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
                and item != 0
            ):
                return True
            if lowered in ("is_error", "iserror") and item is True:
                return True
            if lowered == "error" and item:
                return True
            if (
                lowered == "status"
                and isinstance(item, str)
                and item.lower() in FAILED_STATUSES
            ):
                return True
            if _structured_failure(item):
                return True
    elif isinstance(value, list):
        return any(_structured_failure(item) for item in value)
    return False


def _result_failed(result: dict) -> bool:
    return _structured_failure(result) or any(
        NONZERO_EXIT.search(text) for text in _string_leaves(result)
    )


def _tool_results(value: object) -> list[tuple[str, bool]]:
    """(what the result answers, failed) for every tool result, in conversation order.

    The first element is "id:<call id>", or "name:<function>" for Gemini, whose
    function calls carry no id the CLI must echo.
    """
    found: list[tuple[str, bool]] = []
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "tool_result":  # Anthropic Messages
            found.append((f"id:{value.get('tool_use_id')}", _result_failed(value)))
        elif isinstance(kind, str) and kind in TOOL_RESULT_TYPES:  # Responses
            found.append((f"id:{value.get('call_id')}", _result_failed(value)))
        elif value.get("role") == "tool":  # Chat Completions
            found.append((f"id:{value.get('tool_call_id')}", _result_failed(value)))
        response = value.get("functionResponse")
        if isinstance(response, dict):  # Gemini
            found.append((f"name:{response.get('name')}", _result_failed(response)))
        for item in value.values():
            found.extend(_tool_results(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_tool_results(item))
    return found


def _answers(call_key: str, result_key: str) -> bool:
    if call_key.startswith("gemini:"):
        return result_key == "name:" + call_key.split(":", 2)[1]
    return call_key == result_key


def _round_trip_failure(marker: str, results: list[tuple[str, bool]]) -> str | None:
    """Why this prompt's latest shell call did not complete, or None when it did."""
    now = time.monotonic()
    with _lock:
        latest = _latest.get(marker)
        if latest is None:
            return "no shell call was issued for this prompt"
        call_key, issued_at = latest
        result_key, failed = results[-1]
        if not _answers(call_key, result_key):
            return "the latest tool result does not answer the shell call issued for this prompt"
        if call_key not in _outcomes:
            elapsed = now - issued_at
            if failed:
                _outcomes[call_key] = (
                    "the shell call reported an error or a nonzero exit"
                )
            elif elapsed < TOOL_MIN_SECONDS:
                _outcomes[call_key] = (
                    f"the shell call returned after {elapsed:.1f} s, before '{TOOL_COMMAND}' could finish"
                )
            else:
                _outcomes[call_key] = None
        return _outcomes[call_key]


def _declared_tools(payload: dict) -> list[tuple[str, dict]]:
    """(name, JSON schema) for every function tool, across the four wire shapes."""
    found: list[tuple[str, dict]] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if isinstance(tool.get("function"), dict):  # Chat Completions
            found.append(
                (
                    str(tool["function"].get("name", "")),
                    tool["function"].get("parameters") or {},
                )
            )
        elif "input_schema" in tool:  # Anthropic Messages
            found.append((str(tool.get("name", "")), tool.get("input_schema") or {}))
        elif isinstance(tool.get("functionDeclarations"), list):  # Gemini
            for decl in tool["functionDeclarations"]:
                schema = (
                    decl.get("parametersJsonSchema") or decl.get("parameters") or {}
                )
                found.append((str(decl.get("name", "")), schema))
        elif tool.get("type") == "function":  # Responses
            found.append((str(tool.get("name", "")), tool.get("parameters") or {}))
    return found


def _shell_call(payload: dict) -> tuple[str, dict]:
    tools = _declared_tools(payload)
    by_name = {name.lower(): (name, schema) for name, schema in tools}
    for wanted in SHELL_TOOL_NAMES:
        if wanted in by_name:
            name, schema = by_name[wanted]
            break
    else:
        raise ValueError(f"no shell tool offered; tools: {sorted(by_name)}")
    props = schema.get("properties") or {}
    print(f"shell tool {name}: {json.dumps(schema, sort_keys=True)[:2000]}", flush=True)
    lowered = {k.lower(): k for k in props}
    key = next(
        (lowered[k] for k in ("command", "cmd", "commandline") if k in lowered), None
    )
    if key is None:
        raise ValueError(
            f"shell tool {name!r} has no command parameter: {sorted(props)}"
        )
    args: dict = {
        key: ["bash", "-lc", TOOL_COMMAND]
        if props[key].get("type") == "array"
        else TOOL_COMMAND
    }
    for required in schema.get("required") or []:
        if required in args:
            continue
        kind = (props.get(required) or {}).get("type")
        lname = required.lower()
        if kind == "boolean":
            # Wait for the command: anything asking to background it is off.
            args[required] = not any(word in lname for word in ("background", "async"))
        elif kind in ("integer", "number"):
            args[required] = 30_000
        elif lname in ("cwd", "workdir", "working_directory", "directory", "dir"):
            args[required] = "."
        else:
            args[required] = "Wait six seconds"
    return name, args


def _plan(payload: dict) -> tuple[str, str | tuple[str, dict]]:
    """("text", reply) or ("tool", (name, arguments)) for one generation request.

    The marker goes out only after a successful, full-length result for the
    shell call this endpoint issued. A request offering no tools (a title or
    summary side call) gets neutral text, never the marker.
    """
    text = _reply_text(payload)
    if not text.startswith("LH_SERVED_"):
        return "text", text
    if not payload.get("tools"):
        return "text", "Console served-state check"
    results = _tool_results(
        {key: value for key, value in payload.items() if key != "tools"}
    )
    if not results:
        return "tool", _shell_call(payload)
    failure = _round_trip_failure(text, results)
    if failure:
        print(f"round trip failed: {failure}", flush=True)
        return "text", f"LH_TOOL_ROUND_TRIP_FAILED: {failure}"
    return "text", text


def _openai_usage() -> dict[str, int]:
    return {
        "prompt_tokens": INPUT_TOKENS,
        "completion_tokens": OUTPUT_TOKENS,
        "total_tokens": TOTAL_TOKENS,
    }


def _response_object(response_id: str, model: str, text: str, *, status: str) -> dict:
    part = {"type": "output_text", "text": text, "annotations": []}
    message = {
        "id": f"msg_{response_id}",
        "type": "message",
        "role": "assistant",
        "content": [part],
        "status": "completed" if status == "completed" else "in_progress",
    }
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": [message] if status == "completed" else [],
        "parallel_tool_calls": False,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": (
            {
                "input_tokens": INPUT_TOKENS,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": OUTPUT_TOKENS,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": TOTAL_TOKENS,
            }
            if status == "completed"
            else None
        ),
        "user": None,
    }


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LonghouseMockLLM/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_sse(self, frames: list[tuple[str | None, dict | str]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event, data in frames:
                if event:
                    self.wfile.write(f"event: {event}\n".encode("utf-8"))
                encoded = (
                    data
                    if isinstance(data, str)
                    else json.dumps(data, separators=(",", ":"))
                )
                self.wfile.write(f"data: {encoded}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 10_000_000:
            raise ValueError("request body is too large")
        raw = self.rfile.read(size)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("request body is not JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/", "/healthz", "/api/hello", "/v1/oauth/hello"}:
            self._send_json(200, {"status": "ok"})
            return
        if path.endswith("/models"):
            if "/v1beta/" in path:
                self._send_json(200, self._gemini_models())
            else:
                self._send_json(200, self._models())
            return
        self._send_json(
            404,
            {"error": {"message": "unsupported fake endpoint", "type": "not_found"}},
        )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            payload = self._read_json()
            if path.endswith("/messages/count_tokens"):
                self._send_json(200, {"input_tokens": INPUT_TOKENS})
                return
            if path.endswith("/messages"):
                self._anthropic(payload)
                return
            if path.endswith("/responses"):
                self._responses(payload)
                return
            if path.endswith("/chat/completions"):
                self._chat_completions(payload)
                return
            gemini = GEMINI_GENERATE.search(path)
            if gemini:
                model = unquote(gemini.group(1))
                method = gemini.group(2)
                if method == "countTokens":
                    self._send_json(
                        200, {"totalTokens": INPUT_TOKENS, "cachedContentTokenCount": 0}
                    )
                else:
                    self._gemini(
                        payload, model, stream=method == "streamGenerateContent"
                    )
                return
        except ValueError as exc:
            print(f"POST {path} -> 400 {exc}", flush=True)
            self._send_json(
                400, {"error": {"message": str(exc), "type": "invalid_request_error"}}
            )
            return
        self._send_json(
            404,
            {"error": {"message": "unsupported fake endpoint", "type": "not_found"}},
        )

    @staticmethod
    def _models() -> dict:
        model = {
            "id": "mock-model",
            "object": "model",
            "created": 0,
            "owned_by": "longhouse",
            "type": "model",
            "display_name": "Longhouse Mock Model",
            "created_at": "2026-01-01T00:00:00Z",
        }
        return {
            "data": [model],
            "has_more": False,
            "first_id": model["id"],
            "last_id": model["id"],
        }

    @staticmethod
    def _gemini_models() -> dict:
        return {
            "models": [
                {
                    "name": "models/mock-model",
                    "version": "1",
                    "displayName": "Longhouse Mock Model",
                    "inputTokenLimit": 200000,
                    "outputTokenLimit": 8192,
                    "supportedGenerationMethods": [
                        "generateContent",
                        "streamGenerateContent",
                        "countTokens",
                    ],
                }
            ],
            "nextPageToken": "",
        }

    def _responses(self, payload: dict) -> None:
        kind, plan = _plan(payload)
        print(
            f"POST /responses -> {kind} {plan[0] if kind == 'tool' else ''}", flush=True
        )
        if kind == "tool":
            self._responses_tool(payload, *plan)
            return
        text = plan
        model = str(payload.get("model") or "mock-model")
        response_id = f"resp_{uuid4().hex}"
        if not payload.get("stream", True):
            self._send_json(
                200, _response_object(response_id, model, text, status="completed")
            )
            return

        item_id = f"msg_{uuid4().hex}"
        created = _response_object(response_id, model, text, status="in_progress")
        message = {
            "id": item_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "status": "in_progress",
        }
        part = {"type": "output_text", "text": "", "annotations": []}
        final_message = {
            **message,
            "content": [{"type": "output_text", "text": text, "annotations": []}],
            "status": "completed",
        }
        final_response = _response_object(response_id, model, text, status="completed")
        final_response["output"] = [final_message]
        cut = max(1, len(text) // 2)
        chunks = [text[:cut], text[cut:]] if text[cut:] else [text]
        frames: list[tuple[str | None, dict | str]] = [
            ("response.created", {"type": "response.created", "response": created}),
            (
                "response.in_progress",
                {"type": "response.in_progress", "response": created},
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": message,
                },
            ),
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": part,
                },
            ),
        ]
        frames.extend(
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": chunk,
                },
            )
            for chunk in chunks
        )
        frames.extend(
            [
                (
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": text,
                    },
                ),
                (
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        },
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": final_message,
                    },
                ),
                (
                    "response.completed",
                    {"type": "response.completed", "response": final_response},
                ),
            ]
        )
        self._send_sse(frames)

    def _anthropic(self, payload: dict) -> None:
        kind, plan = _plan(payload)
        print(
            f"POST /messages -> {kind} {plan[0] if kind == 'tool' else ''}", flush=True
        )
        if kind == "tool":
            self._anthropic_tool(payload, *plan)
            return
        text = plan
        model = str(payload.get("model") or "mock-model")
        message_id = f"msg_{uuid4().hex}"
        message = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
        }
        if not payload.get("stream", False):
            self._send_json(200, message)
            return
        cut = max(1, len(text) // 2)
        chunks = [text[:cut], text[cut:]] if text[cut:] else [text]
        frames: list[tuple[str | None, dict | str]] = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        **message,
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
        ]
        frames.extend(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": chunk},
                },
            )
            for chunk in chunks
        )
        frames.extend(
            [
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": OUTPUT_TOKENS},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )
        self._send_sse(frames)

    def _chat_completions(self, payload: dict) -> None:
        kind, plan = _plan(payload)
        print(
            f"POST /chat/completions -> {kind} {plan[0] if kind == 'tool' else ''}",
            flush=True,
        )
        if kind == "tool":
            self._chat_tool(payload, *plan)
            return
        text = plan
        model = str(payload.get("model") or "mock-model")
        completion_id = f"chatcmpl_{uuid4().hex}"
        created = int(time.time())
        if not payload.get("stream", False):
            self._send_json(
                200,
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": _openai_usage(),
                },
            )
            return
        cut = max(1, len(text) // 2)
        chunks = [text[:cut], text[cut:]] if text[cut:] else [text]
        frames: list[tuple[str | None, dict | str]] = [
            (
                None,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": None,
                        }
                    ],
                },
            )
        ]
        frames.extend(
            (
                None,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": chunk}, "finish_reason": None}
                    ],
                },
            )
            for chunk in chunks
        )
        frames.extend(
            [
                (
                    None,
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ),
                (
                    None,
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": _openai_usage(),
                    },
                ),
                (None, "[DONE]"),
            ]
        )
        self._send_sse(frames)

    def _responses_tool(self, payload: dict, name: str, args: dict) -> None:
        model = str(payload.get("model") or "mock-model")
        response_id = f"resp_{uuid4().hex}"
        arguments = json.dumps(args)
        call = {
            "id": f"fc_{uuid4().hex}",
            "type": "function_call",
            "call_id": f"call_{uuid4().hex}",
            "name": name,
            "arguments": arguments,
            "status": "completed",
        }
        _issue(_reply_text(payload), f"id:{call['call_id']}")
        final = _response_object(response_id, model, "", status="completed")
        final["output"] = [call]
        if not payload.get("stream", True):
            self._send_json(200, final)
            return
        created = _response_object(response_id, model, "", status="in_progress")
        ref = {"item_id": call["id"], "output_index": 0}
        self._send_sse(
            [
                ("response.created", {"type": "response.created", "response": created}),
                (
                    "response.in_progress",
                    {"type": "response.in_progress", "response": created},
                ),
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**call, "arguments": "", "status": "in_progress"},
                    },
                ),
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        **ref,
                        "delta": arguments,
                    },
                ),
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        **ref,
                        "arguments": arguments,
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": call,
                    },
                ),
                (
                    "response.completed",
                    {"type": "response.completed", "response": final},
                ),
            ]
        )

    def _anthropic_tool(self, payload: dict, name: str, args: dict) -> None:
        model = str(payload.get("model") or "mock-model")
        block = {
            "type": "tool_use",
            "id": f"toolu_{uuid4().hex}",
            "name": name,
            "input": args,
        }
        _issue(_reply_text(payload), f"id:{block['id']}")
        message = {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [block],
            "model": model,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
        }
        if not payload.get("stream", False):
            self._send_json(200, message)
            return
        start = {
            **message,
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 0},
        }
        self._send_sse(
            [
                ("message_start", {"type": "message_start", "message": start}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {**block, "input": {}},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(args),
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": OUTPUT_TOKENS},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )

    def _chat_tool(self, payload: dict, name: str, args: dict) -> None:
        model = str(payload.get("model") or "mock-model")
        completion_id = f"chatcmpl_{uuid4().hex}"
        created = int(time.time())
        call = {
            "id": f"call_{uuid4().hex}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        _issue(_reply_text(payload), f"id:{call['id']}")
        if not payload.get("stream", False):
            self._send_json(
                200,
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [call],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": _openai_usage(),
                },
            )
            return
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
        }
        self._send_sse(
            [
                (
                    None,
                    {
                        **chunk,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant"},
                                "finish_reason": None,
                            }
                        ],
                    },
                ),
                (
                    None,
                    {
                        **chunk,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": [{"index": 0, **call}]},
                                "finish_reason": None,
                            }
                        ],
                    },
                ),
                (
                    None,
                    {
                        **chunk,
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                        ],
                    },
                ),
                (None, {**chunk, "choices": [], "usage": _openai_usage()}),
                (None, "[DONE]"),
            ]
        )

    def _gemini_payload(
        self, text: str, model: str, *, call: tuple[str, dict] | None = None
    ) -> dict:
        return {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": call[0], "args": call[1]}}]
                        if call
                        else [{"text": text}],
                    },
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": INPUT_TOKENS,
                "candidatesTokenCount": OUTPUT_TOKENS,
                "totalTokenCount": TOTAL_TOKENS,
                "cachedContentTokenCount": 0,
                "thoughtsTokenCount": 0,
            },
            "modelVersion": model.removeprefix("models/"),
        }

    def _gemini(self, payload: dict, model: str, *, stream: bool) -> None:
        kind, plan = _plan(payload)
        print(f"POST gemini -> {kind} {plan[0] if kind == 'tool' else ''}", flush=True)
        if kind == "tool":
            _issue(_reply_text(payload), f"gemini:{plan[0]}:{uuid4().hex}")
            result = self._gemini_payload("", model, call=plan)
        else:
            result = self._gemini_payload(plan, model)
        if stream:
            self._send_sse([(None, result)])
        else:
            self._send_json(200, result)


class ThreadedServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=0, help="listen port (0 chooses a free port)"
    )
    parser.add_argument(
        "--port-file", type=Path, help="write the bound port here after listening"
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    server = ThreadedServer((HOST, args.port), MockHandler)
    if args.port_file:
        args.port_file.write_text(f"{server.server_port}\n", encoding="ascii")
    print(f"Console mock LLM listening on {HOST}:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
