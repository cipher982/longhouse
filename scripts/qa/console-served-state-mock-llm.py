#!/usr/bin/env python3
"""Loopback-only fake LLM endpoint for the Console served-state CI proof.

This serves the upstream wire protocols used by the stock CLI fixtures. The
served-state marker is reconstructed from the request, never embedded here.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

HOST = "127.0.0.1"
INPUT_TOKENS = 32
OUTPUT_TOKENS = 8
TOTAL_TOKENS = INPUT_TOKENS + OUTPUT_TOKENS
PROMPT_MARKER = re.compile(r'concatenate the prefix "([^"]+)" and suffix "([^"]+)"', re.IGNORECASE)
GEMINI_GENERATE = re.compile(r"(?:^|/)models/(.+):(streamGenerateContent|generateContent|countTokens)$")


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
                encoded = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
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
        self._send_json(404, {"error": {"message": "unsupported fake endpoint", "type": "not_found"}})

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
                    self._send_json(200, {"totalTokens": INPUT_TOKENS, "cachedContentTokenCount": 0})
                else:
                    self._gemini(payload, model, stream=method == "streamGenerateContent")
                return
        except ValueError as exc:
            self._send_json(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            return
        self._send_json(404, {"error": {"message": "unsupported fake endpoint", "type": "not_found"}})

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
        return {"data": [model], "has_more": False, "first_id": model["id"], "last_id": model["id"]}

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
                    "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
                }
            ],
            "nextPageToken": "",
        }

    def _responses(self, payload: dict) -> None:
        text = _reply_text(payload)
        model = str(payload.get("model") or "mock-model")
        response_id = f"resp_{uuid4().hex}"
        if not payload.get("stream", True):
            self._send_json(200, _response_object(response_id, model, text, status="completed"))
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
            ("response.in_progress", {"type": "response.in_progress", "response": created}),
            (
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": 0, "item": message},
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
                        "part": {"type": "output_text", "text": text, "annotations": []},
                    },
                ),
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": 0, "item": final_message},
                ),
                ("response.completed", {"type": "response.completed", "response": final_response}),
            ]
        )
        self._send_sse(frames)

    def _anthropic(self, payload: dict) -> None:
        text = _reply_text(payload)
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
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ]
        frames.extend(
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk}},
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
        text = _reply_text(payload)
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
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
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
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
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

    def _gemini_payload(self, text: str, model: str) -> dict:
        return {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
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
        text = _reply_text(payload)
        result = self._gemini_payload(text, model)
        if stream:
            self._send_sse([(None, result)])
        else:
            self._send_json(200, result)



class ThreadedServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="listen port (0 chooses a free port)")
    parser.add_argument("--port-file", type=Path, help="write the bound port here after listening")
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
