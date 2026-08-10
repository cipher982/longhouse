#!/usr/bin/env python3
"""Mock LLM endpoint for hermetic terminal recordings. Stdlib only.

Impersonates provider APIs just well enough to drive one scripted task
turn-by-turn. The provider CLI (the real, pinned binary) is pointed here via
its base-URL override with a dummy key; the TUI rendering is 100% real binary
behavior, only the model text is authored.

Wire shapes served:
  POST /v1/messages               Anthropic Messages SSE (Claude Code)
  POST /v1/messages/count_tokens  trivial token count
  POST /v1/chat/completions       OpenAI chat-completions SSE (codex wire_api
                                  "chat", opencode via OpenRouter baseURL)
  GET  /health                    readiness probe
  GET  /stats                     request counters + expected_main_requests
                                  (the recorder's fail-closed mock gate)

Fixture file = {"turns": [{"text": "...", "tools": [{"name": N, "input": {}}]}]}
The i-th MAIN-lane completion request gets turns[i] (clamped to the last turn,
which must be tool-free so retries/utility calls terminate). Turn index =
number of assistant messages in the request, which is retry-safe. Requests for
"haiku" models (Claude Code utility calls) get a canned one-liner instead.

Usage: python3 mock_llm.py --port 8377 --fixture fixtures/claude.json [--log x.ndjson]
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEXT_CHUNK_WORDS = 3
TEXT_CHUNK_SLEEP = 0.045
TOOL_JSON_SLEEP = 0.03
FIRST_EVENT_DELAY = 0.5

FIXTURE = {"turns": []}
LOG_PATH = None

# Request counters served by /stats. "main" = completion requests on the
# fixture lane (anthropic-main / openai-chat / openai-responses). The recorder
# gates a take on DISTINCT fixture turns served == len(fixture turns): raw
# main-request count over-counts because providers make extra main-model calls
# outside the task conversation (observed Claude Code 2.1.226: a parallel
# 1-message startup warmup and a post-completion conversation summarizer),
# while distinct-turns proves the entire scripted conversation actually
# played out and nothing more is claimed.
MAIN_KINDS = {"anthropic-main", "openai-chat", "openai-responses"}
STATS_LOCK = threading.Lock()
STATS = {"main_requests": 0, "utility_requests": 0, "by_kind": {}}
MAIN_TURNS_SERVED = set()


def log_request(kind, payload, turn_idx):
    with STATS_LOCK:
        STATS["by_kind"][kind] = STATS["by_kind"].get(kind, 0) + 1
        if kind in MAIN_KINDS:
            STATS["main_requests"] += 1
            MAIN_TURNS_SERVED.add(turn_idx)
        else:
            STATS["utility_requests"] += 1
    if not LOG_PATH:
        return
    with open(LOG_PATH, "a") as f:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "turn": turn_idx,
            "model": payload.get("model"),
            "n_messages": len(payload.get("messages", [])),
        }
        # OpenCode sends the executed tool result on its next chat request.
        # Preserve that exact wire payload when calibrating this compatibility lane.
        if kind == "openai-chat":
            entry["request"] = payload
        f.write(json.dumps(entry) + "\n")


def chunk_text(text):
    words = text.split(" ")
    for i in range(0, len(words), TEXT_CHUNK_WORDS):
        chunk = " ".join(words[i:i + TEXT_CHUNK_WORDS])
        if i + TEXT_CHUNK_WORDS < len(words):
            chunk += " "
        yield chunk


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        print(f"[mock] {self.command} {self.path} {fmt % args}", file=sys.stderr)

    # ------------------------------------------------------------ plumbing
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, event, data):
        payload = b""
        if event:
            payload += f"event: {event}\n".encode()
        payload += f"data: {json.dumps(data)}\n\n".encode()
        self.wfile.write(payload)
        self.wfile.flush()

    # ------------------------------------------------------------ routing
    def do_HEAD(self):
        # Claude Code preflight probes /api/hello with HEAD; answer 200.
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send_json({"ok": True, "turns": len(FIXTURE["turns"])})
        elif self.path.startswith("/stats"):
            with STATS_LOCK:
                snap = dict(STATS, by_kind=dict(STATS["by_kind"]))
                snap["main_turns_served"] = sorted(MAIN_TURNS_SERVED)
            snap["distinct_main_turns"] = len(snap["main_turns_served"])
            snap["expected_turns"] = len(FIXTURE["turns"])
            self._send_json(snap)
        elif "/models" in self.path:
            self._send_json({"data": [{"id": "mock-model", "object": "model"}]})
        elif "hello" in self.path:
            # Claude Code connectivity preflight (/api/hello, /v1/oauth/hello).
            self._send_json({"ok": True})
        else:
            self._send_json({"ok": True})

    def do_POST(self):
        payload = self._read_json()
        path = self.path.split("?")[0]
        try:
            if path.endswith("/count_tokens"):
                self._send_json({"input_tokens": 128})
            elif "/messages" in path:
                self.anthropic_messages(payload)
            elif "/chat/completions" in path:
                self.openai_chat(payload)
            elif "/responses" in path:
                self.openai_responses(payload)
            else:
                self._send_json({"ok": True})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------ fixture
    def _turn_for(self, payload):
        msgs = payload.get("messages", [])
        idx = sum(1 for m in msgs if m.get("role") == "assistant")
        turns = FIXTURE["turns"]
        return min(idx, len(turns) - 1), turns[min(idx, len(turns) - 1)]

    # ------------------------------------------------------------ anthropic
    def anthropic_messages(self, payload):
        model = payload.get("model", "mock")
        if "haiku" in model:
            log_request("anthropic-utility", payload, -1)
            self._anthropic_stream(model, {"text": "ok", "tools": []}, utility=True)
            return
        idx, turn = self._turn_for(payload)
        log_request("anthropic-main", payload, idx)
        self._anthropic_stream(model, turn)

    def _anthropic_stream(self, model, turn, utility=False):
        self._start_sse()
        if not utility:
            time.sleep(FIRST_EVENT_DELAY)
        msg_id = f"msg_mock_{int(time.time()*1000)}"
        self._sse("message_start", {
            "type": "message_start",
            "message": {"id": msg_id, "type": "message", "role": "assistant",
                        "model": model, "content": [], "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 214, "output_tokens": 1}},
        })
        block = 0
        out_tokens = 0
        text = turn.get("text") or ""
        if text:
            self._sse("content_block_start", {
                "type": "content_block_start", "index": block,
                "content_block": {"type": "text", "text": ""}})
            for chunk in chunk_text(text):
                self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": block,
                    "delta": {"type": "text_delta", "text": chunk}})
                out_tokens += TEXT_CHUNK_WORDS
                if not utility:
                    time.sleep(TEXT_CHUNK_SLEEP)
            self._sse("content_block_stop", {"type": "content_block_stop", "index": block})
            block += 1
        tools = turn.get("tools") or []
        for i, tool in enumerate(tools):
            self._sse("content_block_start", {
                "type": "content_block_start", "index": block,
                "content_block": {"type": "tool_use", "id": f"toolu_mock_{msg_id}_{i}",
                                   "name": tool["name"], "input": {}}})
            raw = json.dumps(tool["input"])
            for j in range(0, len(raw), 80):
                self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": block,
                    "delta": {"type": "input_json_delta", "partial_json": raw[j:j + 80]}})
                out_tokens += 20
                time.sleep(TOOL_JSON_SLEEP)
            self._sse("content_block_stop", {"type": "content_block_stop", "index": block})
            block += 1
        stop = "tool_use" if tools else "end_turn"
        self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": max(out_tokens, 5)}})
        self._sse("message_stop", {"type": "message_stop"})

    # ------------------------------------------------------------ openai chat
    def openai_chat(self, payload):
        model = payload.get("model", "mock")
        idx, turn = self._turn_for(payload)
        log_request("openai-chat", payload, idx)
        self._start_sse()
        time.sleep(FIRST_EVENT_DELAY)
        created = int(time.time())
        cid = f"chatcmpl-mock{created}"

        def chunk(delta, finish=None):
            self._sse(None, {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        chunk({"role": "assistant", "content": ""})
        text = turn.get("text") or ""
        for piece in chunk_text(text):
            chunk({"content": piece})
            time.sleep(TEXT_CHUNK_SLEEP)
        tools = turn.get("tools") or []
        for i, tool in enumerate(tools):
            chunk({"tool_calls": [{"index": i, "id": f"call_mock_{created}_{i}",
                                    "type": "function",
                                    "function": {"name": tool["name"], "arguments": ""}}]})
            raw = json.dumps(tool["input"])
            for j in range(0, len(raw), 80):
                chunk({"tool_calls": [{"index": i,
                                        "function": {"arguments": raw[j:j + 80]}}]})
                time.sleep(TOOL_JSON_SLEEP)
        chunk({}, finish="tool_calls" if tools else "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ------------------------------------------------------- openai responses
    def _turn_for_responses(self, payload):
        # Each executed tool appends a function_call_output to the input, so the
        # count of those == how many turns have completed == next turn index.
        items = payload.get("input") or []
        idx = sum(1 for it in items
                  if isinstance(it, dict) and it.get("type") == "function_call_output")
        turns = FIXTURE["turns"]
        return min(idx, len(turns) - 1), turns[min(idx, len(turns) - 1)]

    def openai_responses(self, payload):
        model = payload.get("model", "mock")
        idx, turn = self._turn_for_responses(payload)
        log_request("openai-responses", payload, idx)
        self._start_sse()
        time.sleep(FIRST_EVENT_DELAY)
        rid = f"resp_mock_{int(time.time()*1000)}"
        seq = [0]

        def ev(obj):
            obj["sequence_number"] = seq[0]
            seq[0] += 1
            self._sse(obj["type"], obj)

        ev({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        oi = 0
        text = turn.get("text") or ""
        if text:
            item_id = f"msg_{rid}_{oi}"
            ev({"type": "response.output_item.added", "output_index": oi,
                "item": {"id": item_id, "type": "message", "role": "assistant",
                         "status": "in_progress", "content": []}})
            ev({"type": "response.content_part.added", "output_index": oi,
                "item_id": item_id, "content_index": 0,
                "part": {"type": "output_text", "text": ""}})
            for piece in chunk_text(text):
                ev({"type": "response.output_text.delta", "output_index": oi,
                    "item_id": item_id, "content_index": 0, "delta": piece})
                time.sleep(TEXT_CHUNK_SLEEP)
            ev({"type": "response.output_text.done", "output_index": oi,
                "item_id": item_id, "content_index": 0, "text": text})
            ev({"type": "response.output_item.done", "output_index": oi,
                "item": {"id": item_id, "type": "message", "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": text}]}})
            oi += 1
        for tool in (turn.get("tools") or []):
            call_id = f"call_{rid}_{oi}"
            item_id = f"fc_{rid}_{oi}"
            ev({"type": "response.output_item.added", "output_index": oi,
                "item": {"id": item_id, "type": "function_call", "status": "in_progress",
                         "name": tool["name"], "call_id": call_id, "arguments": ""}})
            raw = json.dumps(tool["input"])
            for j in range(0, len(raw), 80):
                ev({"type": "response.function_call_arguments.delta",
                    "output_index": oi, "item_id": item_id, "delta": raw[j:j + 80]})
                time.sleep(TOOL_JSON_SLEEP)
            ev({"type": "response.function_call_arguments.done",
                "output_index": oi, "item_id": item_id, "arguments": raw})
            ev({"type": "response.output_item.done", "output_index": oi,
                "item": {"id": item_id, "type": "function_call", "status": "completed",
                         "name": tool["name"], "call_id": call_id, "arguments": raw}})
            oi += 1
        ev({"type": "response.completed",
            "response": {"id": rid, "status": "completed",
                         "usage": {
                             "input_tokens": 200,
                             "input_tokens_details": {"cached_tokens": 0},
                             "output_tokens": 40,
                             "output_tokens_details": {"reasoning_tokens": 0},
                             "total_tokens": 240}}})


def main():
    global FIXTURE, LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--log", default=None)
    ap.add_argument("--demo-repo", default=None,
                    help="rewrite the fixture's /tmp/demo-repo paths to this per-run scratch path")
    args = ap.parse_args()
    with open(args.fixture) as f:
        raw = f.read()
    if args.demo_repo:
        raw = raw.replace("/tmp/demo-repo", args.demo_repo)
    FIXTURE = json.loads(raw)
    if not FIXTURE.get("turns"):
        raise SystemExit("fixture has no turns")
    last = FIXTURE["turns"][-1]
    if last.get("tools"):
        raise SystemExit("last fixture turn must be tool-free (termination guarantee)")
    LOG_PATH = args.log
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[mock] serving on 127.0.0.1:{args.port} "
          f"({len(FIXTURE['turns'])} turns)", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
