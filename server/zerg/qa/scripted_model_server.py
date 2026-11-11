#!/usr/bin/env python3
"""Loopback-only fake model endpoint used by Longhouse provider QA.

The endpoint serves OpenAI Chat Completions/Responses, Anthropic Messages, and
Gemini. Optional ordered JSON scenarios match input markers; without a script,
the Console served-state behavior remains request-derived.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlsplit
from uuid import uuid4

HOST = "127.0.0.1"
INPUT_TOKENS = 32
OUTPUT_TOKENS = 8
TOTAL_TOKENS = INPUT_TOKENS + OUTPUT_TOKENS
PROMPT_MARKER = re.compile(r'concatenate the prefix "([^"]+)" and suffix "([^"]+)"', re.IGNORECASE)
GEMINI_GENERATE = re.compile(r"(?:^|/)models/(.+):(streamGenerateContent|generateContent|countTokens)$")
SCRIPTED_INPUT_MARKER = re.compile(r"\b(?:LONGHOUSE_PI_PROBE_[a-f0-9]{32}|(?:OMP|PI)_HELM_[A-Z0-9_]+_[a-f0-9]{16})\b")


@dataclass(frozen=True)
class Pacing:
    """Stream a reply one token per delta at `rate` tokens/second after `initial_delay` seconds.

    `tokens` with no scripted reply generates filler words, with a unique marker word
    at token 0 and every `marker_every` tokens after it, so a reader can time the stream.
    """

    rate: float
    initial_delay: float = 0.0
    tokens: int | None = None
    marker_every: int = 20
    marker_prefix: str = "LHPACE"


@dataclass(frozen=True)
class GenerationPlan:
    text: str | None
    tool_call: tuple[str, dict] | None
    turn_id: str | None = None
    pace: Pacing | None = None


_FILLER_WORDS = ("amber", "birch", "cedar", "dune", "ember", "fjord", "grove", "heath", "inlet", "juniper")
_TOKEN_SPLIT = re.compile(r"\s*\S+|\s+$")


def parse_pacing(value: object, *, where: str = "pace") -> Pacing:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    unknown = set(value) - {"rate", "initial_delay", "tokens", "marker_every", "marker_prefix"}
    if unknown:
        raise ValueError(f"{where} has unknown keys: {sorted(unknown)}")

    def number(key: str, default: float | None) -> float | None:
        raw = value.get(key, default)
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise ValueError(f"{where}.{key} must be a non-negative number")
        return float(raw)

    rate = number("rate", None)
    if not rate:
        raise ValueError(f"{where}.rate must be a positive number")
    tokens = number("tokens", None)
    marker_every = number("marker_every", 20)
    prefix = value.get("marker_prefix", "LHPACE")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9]+", prefix):
        raise ValueError(f"{where}.marker_prefix must be alphanumeric")
    if tokens is not None and tokens < 1:
        raise ValueError(f"{where}.tokens must be at least 1")
    if not marker_every or marker_every < 1:
        raise ValueError(f"{where}.marker_every must be at least 1")
    return Pacing(
        rate=rate,
        initial_delay=number("initial_delay", 0.0) or 0.0,
        tokens=int(tokens) if tokens is not None else None,
        marker_every=int(marker_every),
        marker_prefix=prefix,
    )


def pace_marker(pace: Pacing, index: int) -> str:
    return f"{pace.marker_prefix}{index:04d}"


def paced_text(pace: Pacing) -> str:
    """The generated reply for a pace with `tokens` and no scripted text."""
    if pace.tokens is None:
        raise ValueError("pace.tokens is required when the turn has no reply text")
    # A marker at token 0, every `marker_every` after it, and on the last token (so the end of the stream is timeable too).
    words = [
        pace_marker(pace, i) if i % pace.marker_every == 0 or i == pace.tokens - 1 else _FILLER_WORDS[i % len(_FILLER_WORDS)]
        for i in range(pace.tokens)
    ]
    return " ".join(words)


def pace_schedule(pace: Pacing, text: str) -> list[tuple[int, str, float]]:
    """(token index, marker word, seconds after the stream opens) for every marker token."""
    out = []
    for index, token in enumerate(_TOKEN_SPLIT.findall(text)):
        word = token.strip()
        if word.startswith(pace.marker_prefix) and word == pace_marker(pace, index):
            out.append((index, word, pace.initial_delay + index / pace.rate))
    return out


def _text_chunks(text: str, pace: Pacing | None = None) -> list[tuple[str, float]]:
    """Split a reply into (delta, seconds after the stream opens) pairs.

    Unpaced: the historic two halves, both sent at once. Paced: one token per delta.
    """
    if pace is None:
        cut = max(1, len(text) // 2)
        halves = [text[:cut], text[cut:]] if text[cut:] else [text]
        return [(half, 0.0) for half in halves]
    tokens = _TOKEN_SPLIT.findall(text) or [text]
    return [(token, pace.initial_delay + index / pace.rate) for index, token in enumerate(tokens)]


def load_script(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"script is not valid JSON: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("turns"), list):
        raise ValueError("script must be an object with an ordered 'turns' array")

    turns = value["turns"]
    seen_ids: set[str] = set()
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            raise ValueError(f"turn {index} must be an object")
        turn_id = turn.get("id")
        marker = turn.get("match")
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError(f"turn {index} needs a non-empty string id")
        if turn_id in seen_ids:
            raise ValueError(f"duplicate turn id: {turn_id}")
        seen_ids.add(turn_id)
        if not isinstance(marker, str) or not marker:
            raise ValueError(f"turn {turn_id!r} needs a non-empty match marker")
        reply = turn.get("reply")
        if reply is not None and not isinstance(reply, str):
            raise ValueError(f"turn {turn_id!r} reply must be a string")
        tool_call = turn.get("tool_call")
        if tool_call is not None and not isinstance(tool_call, dict):
            raise ValueError(f"turn {turn_id!r} tool_call must be an object")
        history = turn.get("reply_from_history")
        if history is not None:
            if not isinstance(history, dict) or not isinstance(history.get("pattern"), str):
                raise ValueError(f"turn {turn_id!r} reply_from_history needs a regex pattern")
            marker = history.get("marker")
            if marker is not None and not isinstance(marker, str):
                raise ValueError(f"turn {turn_id!r} reply_from_history marker must be a string")
            try:
                pattern = re.compile(history["pattern"])
            except re.error as exc:
                raise ValueError(f"turn {turn_id!r} has an invalid history regex") from exc
            group = history.get("group", 0)
            if isinstance(group, bool) or not isinstance(group, (int, str)):
                raise ValueError(f"turn {turn_id!r} has an invalid history group")
            if isinstance(group, int) and group > pattern.groups:
                raise ValueError(f"turn {turn_id!r} history group is not present")
            if isinstance(group, str) and group not in pattern.groupindex:
                raise ValueError(f"turn {turn_id!r} history group is not present")
        pace = turn.get("pace")
        if pace is not None:
            parsed = parse_pacing(pace, where=f"turn {turn_id!r} pace")
            if reply is None and parsed.tokens is None and history is None:
                raise ValueError(f"turn {turn_id!r} pace needs tokens or a reply")
        if reply is None and tool_call is None and history is None and pace is None:
            raise ValueError(f"turn {turn_id!r} must contain a reply or tool_call")
    return turns, hashlib.sha256(raw).hexdigest()


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


def _latest_user_marker(value: object) -> str | None:
    for text in reversed(_user_text(value)):
        matches = tuple(SCRIPTED_INPUT_MARKER.finditer(text))
        if matches:
            return matches[-1].group()
    return None


def _has_user_prompt(payload: dict) -> bool:
    return any(text.strip() for text in _user_text(payload)) or (isinstance(payload.get("input"), str) and bool(payload["input"].strip()))


def _responses_tool_result_only(payload: dict) -> bool:
    items = payload.get("input")
    return (
        isinstance(items, list) and bool(items) and all(isinstance(item, dict) and item.get("type") in TOOL_RESULT_TYPES for item in items)
    )


def _latest_input_text(value: object) -> str:
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            role = item.get("role")
            if role in ("user", "tool", "function"):
                found.append("\n".join(_string_leaves(item.get("content", item.get("parts", item.get("output"))))))
                return
            if isinstance(item.get("type"), str) and item["type"] in TOOL_RESULT_TYPES:
                found.append("\n".join(_string_leaves(item)))
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if found:
        return found[-1].strip()
    if isinstance(value, dict) and isinstance(value.get("input"), str):
        return value["input"].strip()
    return ""


def _assistant_history_text(value: object) -> list[str]:
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if item.get("role") in ("assistant", "model"):
                found.append("\n".join(_string_leaves(item.get("content", item.get("parts", item.get("text"))))))
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
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
_outcomes: dict[str, str | None] = {}  # call key -> failure reason, None when it succeeded
_lock = threading.Lock()


def _issue(marker: str, key: str) -> None:
    with _lock:
        _latest[marker] = (key, time.monotonic())


def _structured_failure(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if lowered in EXIT_FIELDS and isinstance(item, (int, float)) and not isinstance(item, bool) and item != 0:
                return True
            if lowered in ("is_error", "iserror") and item is True:
                return True
            if lowered == "error" and item:
                return True
            if lowered == "status" and isinstance(item, str) and item.lower() in FAILED_STATUSES:
                return True
            if _structured_failure(item):
                return True
    elif isinstance(value, list):
        return any(_structured_failure(item) for item in value)
    return False


def _result_failed(result: dict) -> bool:
    return _structured_failure(result) or any(NONZERO_EXIT.search(text) for text in _string_leaves(result))


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
        answering = [failed for result_key, failed in results if _answers(call_key, result_key)]
        if not answering:
            return "no tool result answers the shell call issued for this prompt"
        failed = any(answering)
        if call_key not in _outcomes:
            elapsed = now - issued_at
            if failed:
                _outcomes[call_key] = "the shell call reported an error or a nonzero exit"
            elif elapsed < TOOL_MIN_SECONDS:
                _outcomes[call_key] = f"the shell call returned after {elapsed:.1f} s, before '{TOOL_COMMAND}' could finish"
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
                schema = decl.get("parametersJsonSchema") or decl.get("parameters") or {}
                found.append((str(decl.get("name", "")), schema))
        elif tool.get("type") == "function":  # Responses
            found.append((str(tool.get("name", "")), tool.get("parameters") or {}))
    return found


def _shell_call(
    payload: dict,
    command: str = TOOL_COMMAND,
    preferred_name: str | None = None,
    overrides: dict | None = None,
) -> tuple[str, dict]:
    tools = _declared_tools(payload)
    by_name = {name.lower(): (name, schema) for name, schema in tools}
    if preferred_name and preferred_name.lower() in by_name:
        name, schema = by_name[preferred_name.lower()]
    else:
        for wanted in SHELL_TOOL_NAMES:
            if wanted in by_name:
                name, schema = by_name[wanted]
                break
        else:
            raise ValueError(f"no shell tool offered; tools: {sorted(by_name)}")
    props = schema.get("properties") or {}
    print(f"shell tool {name}: {json.dumps(schema, sort_keys=True)[:2000]}", flush=True)
    lowered = {k.lower(): k for k in props}
    key = next((lowered[k] for k in ("command", "cmd", "commandline") if k in lowered), None)
    if key is None:
        raise ValueError(f"shell tool {name!r} has no command parameter: {sorted(props)}")
    args: dict = dict(overrides or {})
    args[key] = ["bash", "-lc", command] if props[key].get("type") == "array" else command
    for required in schema.get("required") or []:
        if required in args:
            continue
        kind = (props.get(required) or {}).get("type")
        lname = required.lower()
        if kind == "boolean":
            args[required] = not any(word in lname for word in ("background", "async"))
        elif kind in ("integer", "number"):
            args[required] = 30_000
        elif lname in ("cwd", "workdir", "working_directory", "directory", "dir"):
            args[required] = "."
        else:
            args[required] = "Wait six seconds"
    return name, args


def _scripted_tool_call(payload: dict, spec: dict) -> tuple[str, dict]:
    name = spec.get("name")
    arguments = spec.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("scripted tool_call arguments must be an object")
    command = spec.get("command")
    if command is None:
        command = next(
            (value for key, value in arguments.items() if key.lower() in ("command", "cmd", "commandline")),
            None,
        )
    is_shell = spec.get("type") == "shell" or (
        spec.get("type") is None and (name is None or isinstance(name, str) and name.lower() in SHELL_TOOL_NAMES)
    )
    if is_shell:
        if not isinstance(command, str):
            raise ValueError("scripted shell tool_call needs a string command")
        overrides = {key: value for key, value in arguments.items() if key.lower() not in ("command", "cmd", "commandline")}
        return _shell_call(
            payload,
            command,
            preferred_name=name if isinstance(name, str) else None,
            overrides=overrides,
        )
    offered = {tool_name for tool_name, _schema in _declared_tools(payload)}
    if not isinstance(name, str) or name not in offered:
        raise ValueError(f"scripted tool {name!r} is not among offered tools")
    return name, arguments


def _history_value(payload: dict, spec: dict) -> str:
    marker = spec.get("marker")
    pattern = re.compile(spec["pattern"])
    group = spec.get("group", 0)
    for text in reversed(_assistant_history_text(payload)):
        if isinstance(marker, str) and marker not in text:
            continue
        match = pattern.search(text)
        if match:
            try:
                return match.group(group)
            except (IndexError, KeyError) as exc:
                raise ValueError("reply_from_history group is not present") from exc
    raise ValueError("reply_from_history pattern did not match assistant history")


def _scripted_plan(payload: dict, turns: list[dict]) -> GenerationPlan | None:
    latest = _latest_input_text(payload)
    any_user = "\n".join(_user_text(payload))
    for turn in turns:
        # "any_user" matches the marker anywhere in the user messages, for a provider whose last user message is
        # context it appended after the person's prompt (Antigravity).
        haystack = any_user if turn.get("match_scope") == "any_user" else latest
        if turn["match"] not in haystack:
            continue
        reply = turn.get("reply")
        history = turn.get("reply_from_history")
        if history is not None:
            value = _history_value(payload, history)
            reply = reply.replace("{{history}}", value) if reply is not None else value
        if isinstance(reply, str) and "{{input_marker}}" in reply:
            marker = _latest_user_marker(payload)
            if marker is None:
                raise ValueError("scripted reply input-marker template has no matching marker in a user message")
            reply = reply.replace("{{input_marker}}", marker)
        tool_spec = turn.get("tool_call")
        tool_call = _scripted_tool_call(payload, tool_spec) if tool_spec is not None else None
        pace = parse_pacing(turn["pace"]) if turn.get("pace") is not None else None
        if pace is not None and reply is None:
            reply = paced_text(pace)
        return GenerationPlan(reply, tool_call, turn["id"], pace)
    return None


def _default_generation_plan(payload: dict) -> GenerationPlan:
    """Reconstruct the old served-state default response from its request."""
    text = _reply_text(payload)
    if not text.startswith("LH_SERVED_"):
        return GenerationPlan(text, None)
    if not payload.get("tools"):
        return GenerationPlan("Console served-state check", None)
    results = _tool_results({key: value for key, value in payload.items() if key != "tools"})
    if not results:
        return GenerationPlan(None, _shell_call(payload))
    failure = _round_trip_failure(text, results)
    if failure:
        print(f"round trip failed: {failure}", flush=True)
        return GenerationPlan(f"LH_TOOL_ROUND_TRIP_FAILED: {failure}", None)
    return GenerationPlan(text, None)


def _plan(payload: dict) -> tuple[str, str | tuple[str, dict]]:
    """Compatibility wrapper for the pre-script default planner."""
    plan = _default_generation_plan(payload)
    if plan.tool_call:
        return "tool", plan.tool_call
    return "text", plan.text or ""


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

    def _send_json(self, status: int, payload: dict, *, model_request_id: int | None = None) -> None:
        try:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if model_request_id is not None:
                if 200 <= status < 300:
                    self.server.mark_model_request_served(model_request_id)
                else:
                    self.server.mark_model_request_failed(model_request_id)
            self.wfile.write(body)
        except Exception:
            if model_request_id is not None:
                self.server.mark_model_request_failed(model_request_id)
            raise
        finally:
            self.close_connection = True

    def _send_sse(
        self,
        frames: list[tuple],
        *,
        model_request_id: int | None = None,
    ) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            if model_request_id is not None and not frames:
                self.server.mark_model_request_served(model_request_id)
            started = time.monotonic()
            for index, frame in enumerate(frames):
                event, data = frame[0], frame[1]
                at = frame[2] if len(frame) > 2 else 0.0
                wait = started + at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                if model_request_id is not None and index == len(frames) - 1:
                    self.server.mark_model_request_served(model_request_id)
                if event:
                    self.wfile.write(f"event: {event}\n".encode("utf-8"))
                encoded = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
                self.wfile.write(f"data: {encoded}\n\n".encode("utf-8"))
                self.wfile.flush()
                if at > 0:
                    self.server.log_paced_frame(time.monotonic(), encoded)
        except (BrokenPipeError, ConnectionResetError):
            if model_request_id is not None:
                self.server.mark_model_request_failed(model_request_id)
        except Exception:
            if model_request_id is not None:
                self.server.mark_model_request_failed(model_request_id)
            raise
        finally:
            self.close_connection = True

    def _send_model_json(self, payload: dict, turn_id: str | None) -> None:
        request_id = self.server.begin_model_request(turn_id)
        self._send_json(200, payload, model_request_id=request_id)

    def _send_model_sse(self, frames: list[tuple], turn_id: str | None) -> None:
        request_id = self.server.begin_model_request(turn_id)
        self._send_sse(frames, model_request_id=request_id)

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

    def _generation_plan(self, payload: dict) -> GenerationPlan:
        plan = _scripted_plan(payload, self.server.script_turns)
        if plan is None:
            if self.server.script_sha256 is not None and _responses_tool_result_only(payload):
                raise ValueError("scripted Responses tool-result continuation did not match a fixture turn")
            plan = _default_generation_plan(payload)
        return plan

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/_scripted_model/receipt":
            self._send_json(200, self.server.receipt())
            return
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
                    self._send_json(200, {"totalTokens": INPUT_TOKENS, "cachedContentTokenCount": 0})
                else:
                    self._gemini(payload, model, stream=method == "streamGenerateContent")
                return
        except ValueError as exc:
            print(f"POST {path} -> 400 {exc}", flush=True)
            self._send_json(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
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
        plan = self._generation_plan(payload)
        kind = "tool" if plan.tool_call else "text"
        print(
            f"POST /responses -> {kind} {plan.tool_call[0] if plan.tool_call else ''}",
            flush=True,
        )
        if plan.tool_call:
            self._responses_tool(payload, *plan.tool_call, text=plan.text, turn_id=plan.turn_id, pace=plan.pace)
            return
        text = plan.text or ""
        pace = plan.pace
        model = str(payload.get("model") or "mock-model")
        response_id = f"resp_{uuid4().hex}"
        if not payload.get("stream", True):
            self._send_model_json(_response_object(response_id, model, text, status="completed"), plan.turn_id)
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
        chunks = _text_chunks(text, pace)
        frames: list[tuple] = [
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
                at,
            )
            for chunk, at in chunks
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
        self._send_model_sse(frames, plan.turn_id)

    def _anthropic(self, payload: dict) -> None:
        plan = self._generation_plan(payload)
        kind = "tool" if plan.tool_call else "text"
        print(
            f"POST /messages -> {kind} {plan.tool_call[0] if plan.tool_call else ''}",
            flush=True,
        )
        if plan.tool_call:
            self._anthropic_tool(payload, *plan.tool_call, text=plan.text, turn_id=plan.turn_id, pace=plan.pace)
            return
        text = plan.text or ""
        pace = plan.pace
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
            self._send_model_json(message, plan.turn_id)
            return
        chunks = _text_chunks(text, pace)
        frames: list[tuple] = [
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
                at,
            )
            for chunk, at in chunks
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
        self._send_model_sse(frames, plan.turn_id)

    def _chat_completions(self, payload: dict) -> None:
        plan = self._generation_plan(payload)
        kind = "tool" if plan.tool_call else "text"
        print(
            f"POST /chat/completions -> {kind} {plan.tool_call[0] if plan.tool_call else ''}",
            flush=True,
        )
        if plan.tool_call:
            self._chat_tool(payload, *plan.tool_call, text=plan.text, turn_id=plan.turn_id, pace=plan.pace)
            return
        text = plan.text or ""
        pace = plan.pace
        model = str(payload.get("model") or "mock-model")
        completion_id = f"chatcmpl_{uuid4().hex}"
        created = int(time.time())
        if not payload.get("stream", False):
            self._send_model_json(
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
                plan.turn_id,
            )
            return
        chunks = _text_chunks(text, pace)
        frames: list[tuple] = [
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
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                },
                at,
            )
            for chunk, at in chunks
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
        self._send_model_sse(frames, plan.turn_id)

    def _responses_tool(
        self,
        payload: dict,
        name: str,
        args: dict,
        *,
        text: str | None = None,
        turn_id: str | None = None,
        pace: Pacing | None = None,
    ) -> None:
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
        if turn_id is None and _has_user_prompt(payload):
            _issue(_reply_text(payload), f"id:{call['call_id']}")
        text_item = {
            "id": f"msg_{response_id}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
            "status": "completed",
        }
        final = _response_object(response_id, model, text or "", status="completed")
        final["output"] = ([text_item] if text is not None else []) + [call]
        if not payload.get("stream", True):
            self._send_model_json(final, turn_id)
            return

        created = _response_object(response_id, model, text or "", status="in_progress")
        call_index = 1 if text is not None else 0
        frames: list[tuple] = [
            ("response.created", {"type": "response.created", "response": created}),
            (
                "response.in_progress",
                {"type": "response.in_progress", "response": created},
            ),
        ]
        if text is not None:
            item_id = text_item["id"]
            message = {**text_item, "content": [], "status": "in_progress"}
            part = {"type": "output_text", "text": "", "annotations": []}
            chunks = _text_chunks(text, pace)
            frames.extend(
                [
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
            )
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
                    at,
                )
                for chunk, at in chunks
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
                            "item": text_item,
                        },
                    ),
                ]
            )
        ref = {"item_id": call["id"], "output_index": call_index}
        frames.extend(
            [
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": call_index,
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
                        "output_index": call_index,
                        "item": call,
                    },
                ),
                (
                    "response.completed",
                    {"type": "response.completed", "response": final},
                ),
            ]
        )
        self._send_model_sse(frames, turn_id)

    def _anthropic_tool(
        self,
        payload: dict,
        name: str,
        args: dict,
        *,
        text: str | None = None,
        turn_id: str | None = None,
        pace: Pacing | None = None,
    ) -> None:
        model = str(payload.get("model") or "mock-model")
        block = {
            "type": "tool_use",
            "id": f"toolu_{uuid4().hex}",
            "name": name,
            "input": args,
        }
        if turn_id is None and _has_user_prompt(payload):
            _issue(_reply_text(payload), f"id:{block['id']}")
        content = ([{"type": "text", "text": text}] if text is not None else []) + [block]
        message = {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": model,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
        }
        if not payload.get("stream", False):
            self._send_model_json(message, turn_id)
            return
        start = {
            **message,
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 0},
        }
        frames: list[tuple] = [("message_start", {"type": "message_start", "message": start})]
        tool_index = 0
        if text is not None:
            chunks = _text_chunks(text, pace)
            frames.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            frames.extend(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                    at,
                )
                for chunk, at in chunks
            )
            frames.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
            tool_index = 1
        frames.extend(
            [
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": tool_index,
                        "content_block": {**block, "input": {}},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": tool_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(args),
                        },
                    },
                ),
                (
                    "content_block_stop",
                    {"type": "content_block_stop", "index": tool_index},
                ),
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
        self._send_model_sse(frames, turn_id)

    def _chat_tool(
        self,
        payload: dict,
        name: str,
        args: dict,
        *,
        text: str | None = None,
        turn_id: str | None = None,
        pace: Pacing | None = None,
    ) -> None:
        model = str(payload.get("model") or "mock-model")
        completion_id = f"chatcmpl_{uuid4().hex}"
        created = int(time.time())
        call = {
            "id": f"call_{uuid4().hex}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        if turn_id is None and _has_user_prompt(payload):
            _issue(_reply_text(payload), f"id:{call['id']}")
        if not payload.get("stream", False):
            self._send_model_json(
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
                                "content": text,
                                "tool_calls": [call],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": _openai_usage(),
                },
                turn_id,
            )
            return
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
        }
        frames: list[tuple] = [
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
            )
        ]
        if text is not None:
            chunks = _text_chunks(text, pace)
            frames.extend(
                (
                    None,
                    {
                        **chunk,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": content},
                                "finish_reason": None,
                            }
                        ],
                    },
                    at,
                )
                for content, at in chunks
            )
        frames.extend(
            [
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
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    },
                ),
                (None, {**chunk, "choices": [], "usage": _openai_usage()}),
                (None, "[DONE]"),
            ]
        )
        self._send_model_sse(frames, turn_id)

    def _gemini_payload(self, text: str | None, model: str, *, call: tuple[str, dict] | None = None) -> dict:
        parts = [{"text": text}] if text is not None else []
        if call:
            parts.append({"functionCall": {"name": call[0], "args": call[1]}})
        return {
            "candidates": [
                {
                    "content": {"role": "model", "parts": parts},
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
        plan = self._generation_plan(payload)
        kind = "tool" if plan.tool_call else "text"
        print(
            f"POST gemini -> {kind} {plan.tool_call[0] if plan.tool_call else ''}",
            flush=True,
        )
        if plan.tool_call and plan.turn_id is None and _has_user_prompt(payload):
            _issue(_reply_text(payload), f"gemini:{plan.tool_call[0]}:{uuid4().hex}")
        result = self._gemini_payload(plan.text, model, call=plan.tool_call)
        if stream and plan.pace is not None and plan.text is not None:
            chunks = _text_chunks(plan.text, plan.pace)
            frames = []
            for index, (chunk, at) in enumerate(chunks):
                last = index == len(chunks) - 1
                piece = self._gemini_payload(chunk, model, call=plan.tool_call if last else None)
                if not last:
                    del piece["candidates"][0]["finishReason"]
                    del piece["usageMetadata"]
                frames.append((None, piece, at))
            self._send_model_sse(frames, plan.turn_id)
        elif stream:
            self._send_model_sse([(None, result)], plan.turn_id)
        else:
            self._send_model_json(result, plan.turn_id)


class ThreadedServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address,
        request_handler_class,
        bind_and_activate: bool = True,
        *,
        script_turns: list[dict] | None = None,
        script_sha256: str | None = None,
        receipt_file: Path | None = None,
        stream_log: Path | None = None,
    ):
        super().__init__(server_address, request_handler_class, bind_and_activate)
        self.stream_log = stream_log
        self._stream_log_lock = threading.Lock()
        self.script_turns = script_turns or []
        self.script_sha256 = script_sha256
        self.receipt_file = receipt_file
        self.endpoint = f"http://{HOST}:{self.server_port}"
        self._requests: list[dict] = []
        self._receipt_lock = threading.Lock()
        self._write_receipt_locked()

    def _receipt_locked(self) -> dict:
        return {
            "scripted_model": {
                "endpoint": self.endpoint,
                "script_sha256": self.script_sha256,
                "served_requests": sum(request["status"] == "served" for request in self._requests),
            },
            "requests": [dict(request) for request in self._requests],
        }

    def receipt(self) -> dict:
        with self._receipt_lock:
            return self._receipt_locked()

    def log_paced_frame(self, sent_at: float, encoded: str) -> None:
        """Append (time.monotonic() after the write, frame) for each paced frame, so a reader can time the model side."""
        if self.stream_log is None:
            return
        with self._stream_log_lock, self.stream_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": sent_at, "frame": encoded}, separators=(",", ":")) + "\n")

    def begin_model_request(self, turn_id: str | None) -> int:
        with self._receipt_lock:
            request_id = len(self._requests) + 1
            self._requests.append({"request_id": request_id, "matched_turn_id": turn_id, "status": "pending"})
            self._write_receipt_locked()
            return request_id

    def mark_model_request_served(self, request_id: int) -> None:
        self._set_model_request_status(request_id, "served")

    def mark_model_request_failed(self, request_id: int) -> None:
        self._set_model_request_status(request_id, "failed")

    def _set_model_request_status(self, request_id: int, status: str) -> None:
        with self._receipt_lock:
            if request_id < 1 or request_id > len(self._requests):
                raise RuntimeError(f"unknown scripted model request id: {request_id}")
            request = self._requests[request_id - 1]
            if request["status"] == "failed":
                return
            request["status"] = status
            self._write_receipt_locked()

    def _write_receipt_locked(self) -> None:
        if self.receipt_file is None:
            return
        self.receipt_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt_file.with_name(f".{self.receipt_file.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(self._receipt_locked(), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.receipt_file)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="listen port (0 chooses a free port)")
    parser.add_argument("--port-file", type=Path, help="write the bound port here after listening")
    parser.add_argument(
        "--script",
        type=Path,
        default=os.environ.get("LONGHOUSE_SCRIPTED_MODEL_SCRIPT") or None,
        help="ordered JSON scenario file (default: LONGHOUSE_SCRIPTED_MODEL_SCRIPT)",
    )
    parser.add_argument(
        "--receipt-file",
        type=Path,
        default=os.environ.get("LONGHOUSE_SCRIPTED_MODEL_RECEIPT") or None,
        help="write the current run receipt to this file",
    )
    parser.add_argument(
        "--stream-log",
        type=Path,
        default=os.environ.get("LONGHOUSE_SCRIPTED_MODEL_STREAM_LOG") or None,
        help="append one JSON line (monotonic send time, frame) per paced frame written",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    script_turns: list[dict] = []
    script_sha256 = None
    if args.script is not None:
        try:
            script_turns, script_sha256 = load_script(args.script)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))

    server = ThreadedServer(
        (HOST, args.port),
        MockHandler,
        script_turns=script_turns,
        script_sha256=script_sha256,
        receipt_file=args.receipt_file,
        stream_log=args.stream_log,
    )
    if args.port_file:
        # Atomic: a reader polling for the file must never see it empty.
        partial = args.port_file.with_name(args.port_file.name + ".partial")
        partial.write_text(f"{server.server_port}\n", encoding="ascii")
        partial.replace(args.port_file)
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
