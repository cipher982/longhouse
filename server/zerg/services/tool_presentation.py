"""Deterministic read-time presentation for provider tool calls.

Raw provider evidence stays authoritative.  This module projects a disposable,
versioned reading lens and never mutates stored events.  Codex custom ``exec``
wrappers are parsed with a bounded scanner; transcript text is never executed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PRESENTATION_VERSION = 1
MAX_WRAPPER_CHARS = 200_000
MAX_WRAPPER_CALLS = 32
MAX_LITERAL_DEPTH = 8

_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _get_default_rules_path() -> Path:
    packaged_path = Path(__file__).resolve().parents[1] / "_config" / "tool-tiers.json"
    if packaged_path.exists():
        return packaged_path
    return Path(__file__).resolve().parents[3] / "config" / "tool-tiers.json"


DEFAULT_RULES_PATH = _get_default_rules_path()


@lru_cache(maxsize=4)
def _load_rules(path: str = str(DEFAULT_RULES_PATH)) -> dict[str, Any]:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not isinstance(value.get("tools"), dict):
        raise ValueError(f"invalid tool presentation rules: {path}")
    return value


def clear_tool_presentation_cache() -> None:
    _load_rules.cache_clear()


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _scan_quoted(text: str, index: int) -> int | None:
    quote = text[index]
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return None


def _scan_balanced(text: str, index: int, opener: str, closer: str) -> int | None:
    if index >= len(text) or text[index] != opener:
        return None
    depth = 1
    index += 1
    while index < len(text):
        char = text[index]
        if char in {'"', "'", "`"}:
            end = _scan_quoted(text, index)
            if end is None:
                return None
            index = end
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return None
            index = end + 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _split_top_level(text: str, delimiter: str = ",") -> list[str] | None:
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    index = 0
    while index < len(text):
        char = text[index]
        if char in {'"', "'", "`"}:
            end = _scan_quoted(text, index)
            if end is None:
                return None
            index = end
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                return None
        elif char == delimiter and not stack:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    if stack:
        return None
    parts.append(text[start:].strip())
    return parts


def _split_property(text: str) -> tuple[str, str] | None:
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    index = 0
    while index < len(text):
        char = text[index]
        if char in {'"', "'", "`"}:
            end = _scan_quoted(text, index)
            if end is None:
                return None
            index = end
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                return None
        elif char == ":" and not stack:
            return text[:index].strip(), text[index + 1 :].strip()
        index += 1
    return None


def _parse_string(value: str) -> str | None:
    if len(value) < 2 or value[0] not in {'"', "'", "`"} or value[-1] != value[0]:
        return None
    if value[0] == "`" and "${" in value:
        return None
    try:
        if value[0] == '"':
            parsed = json.loads(value)
        elif value[0] == "'":
            parsed = ast.literal_eval(value)
        else:
            parsed = value[1:-1].replace("\\`", "`")
    except (ValueError, SyntaxError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, str) else None


def _parse_js_literal(value: str, *, depth: int = 0) -> tuple[Any, bool]:
    value = value.strip()
    if depth > MAX_LITERAL_DEPTH or not value:
        return value, False
    parsed_string = _parse_string(value)
    if parsed_string is not None:
        return parsed_string, True
    if value == "true":
        return True, True
    if value == "false":
        return False, True
    if value == "null":
        return None, True
    if value == "undefined":
        return None, False
    if _NUMBER.fullmatch(value):
        try:
            return (float(value) if any(c in value for c in ".eE") else int(value)), True
        except ValueError:
            return value, False
    if value.startswith("{") and value.endswith("}"):
        parts = _split_top_level(value[1:-1])
        if parts is None:
            return value, False
        result: dict[str, Any] = {}
        complete = True
        for part in parts:
            if not part:
                continue
            prop = _split_property(part)
            if prop is None:
                complete = False
                continue
            raw_key, raw_value = prop
            key = _parse_string(raw_key)
            if key is None and _IDENTIFIER.fullmatch(raw_key):
                key = raw_key
            if key is None:
                complete = False
                continue
            parsed, parsed_ok = _parse_js_literal(raw_value, depth=depth + 1)
            result[key] = parsed
            complete = complete and parsed_ok
        return result, complete
    if value.startswith("[") and value.endswith("]"):
        parts = _split_top_level(value[1:-1])
        if parts is None:
            return value, False
        result = []
        complete = True
        for part in parts:
            parsed, parsed_ok = _parse_js_literal(part, depth=depth + 1)
            result.append(parsed)
            complete = complete and parsed_ok
        return result, complete
    return value, False


def _resolve_local_literal(source: str, identifier: str, before: int) -> tuple[Any, bool]:
    """Resolve a simple preceding literal used as a tool argument."""

    pattern = re.compile(rf"(?:const|let|var)\s+{re.escape(identifier)}\s*=\s*")
    matches = list(pattern.finditer(source, 0, before))
    if not matches:
        return identifier, False
    start = matches[-1].end()
    if start >= before or source[start] not in {'"', "'", "`"}:
        return identifier, False
    end = _scan_quoted(source, start)
    if end is None or end > before:
        return identifier, False
    return _parse_js_literal(source[start:end])


def extract_codex_wrapper_calls(source: str) -> tuple[list[dict[str, Any]], bool]:
    """Return bounded ``tools.<method>(...)`` calls without executing source."""

    if not source or len(source) > MAX_WRAPPER_CHARS:
        return [], False
    calls: list[dict[str, Any]] = []
    complete = True
    index = 0
    while index < len(source):
        char = source[index]
        if char in {'"', "'", "`"}:
            end = _scan_quoted(source, index)
            if end is None:
                return calls, False
            index = end
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                return calls, False
            index = end + 2
            continue
        if not source.startswith("tools.", index):
            index += 1
            continue
        name_start = index + len("tools.")
        match = _IDENTIFIER.match(source, name_start)
        if match is None:
            complete = False
            index = name_start
            continue
        method = match.group(0)
        paren = _skip_space(source, match.end())
        if paren >= len(source) or source[paren] != "(":
            complete = False
            index = match.end()
            continue
        end = _scan_balanced(source, paren, "(", ")")
        if end is None:
            return calls, False
        arguments = source[paren + 1 : end - 1]
        first_arg = (_split_top_level(arguments) or [arguments])[0]
        tool_input, input_complete = _parse_js_literal(first_arg)
        if not input_complete and _IDENTIFIER.fullmatch(first_arg.strip()):
            tool_input, input_complete = _resolve_local_literal(source, first_arg.strip(), index)
        if method.lower() == "apply_patch" and input_complete and isinstance(tool_input, str):
            tool_input = {"patch": tool_input}
        calls.append(
            {
                "tool_name": method,
                "tool_input_json": tool_input,
                "input_complete": input_complete,
                "source_span": [index, end],
                "result_forwarded": _wrapper_result_is_forwarded(source, index, end),
            }
        )
        if len(calls) >= MAX_WRAPPER_CALLS:
            return calls, False
        complete = complete and input_complete
        index = end
    return calls, complete and bool(calls)


def _wrapper_result_is_forwarded(source: str, call_start: int, call_end: int) -> bool:
    """Prove that a single nested result is exposed by the enclosing wrapper.

    This deliberately recognizes only the generated Codex wrapper shape: an
    assigned awaited call followed by one of the result emitters. Anything
    dynamic stays visible as the enclosing ``exec``.
    """

    prefix = source[max(0, call_start - 96) : call_start]
    if re.search(r"(?:text|image|generatedImage)\s*\(\s*await\s*$", prefix):
        return bool(re.match(r"\s*\)\s*;?", source[call_end:]))
    assignment = re.search(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*await\s*$", prefix)
    if assignment is None:
        return False
    variable = re.escape(assignment.group(1))
    suffix = source[call_end : min(len(source), call_end + 512)]
    emitter = re.search(r"(?:text|image|generatedImage)\s*\((?P<body>[\s\S]*?)\)\s*;?", suffix)
    return bool(emitter and re.search(rf"\b{variable}\b", emitter.group("body")))


def _mcp_parts(tool_name: str) -> tuple[str, str] | None:
    parts = tool_name.split("__")
    if len(parts) == 3 and parts[0] == "mcp":
        return parts[1], parts[2]
    return None


def _base_projection(tool_name: str, tool_input_json: Any, *, rules_path: Path) -> dict[str, Any]:
    rules = _load_rules(str(rules_path.resolve()))
    tools = rules["tools"]
    exact = tools.get(tool_name)
    if exact is None:
        lower = tool_name.lower()
        exact = next((value for name, value in tools.items() if name.lower() == lower), None)
    mcp = _mcp_parts(tool_name)
    if exact is not None:
        meta = exact
        disposition = "exact"
        label = str(meta["label"])
        icon = str(meta["icon"])
        color = str(meta["color"])
        tier = str(meta["tier"])
        aggregate = meta.get("aggregate", rules.get("default_aggregate"))
        namespace = None
    elif mcp is not None:
        namespace, method = mcp
        namespace_key = namespace.lower()
        namespace_meta = None
        for prefix, candidate in rules.get("mcp_namespaces", {}).items():
            parts = re.split(r"[-_]", namespace_key)
            if (
                namespace_key == prefix
                or prefix in parts
                or namespace_key.startswith(f"{prefix}-")
                or namespace_key.startswith(f"{prefix}_")
            ):
                namespace_meta = candidate
                break
        namespace_meta = namespace_meta or {"icon": "M", "color": "muted"}
        disposition = "generic"
        label = method
        icon = str(namespace_meta["icon"])
        color = str(namespace_meta["color"])
        tier = str(rules.get("mcp_default_tier", "noise"))
        aggregate = rules.get("mcp_default_aggregate")
    else:
        namespace = None
        disposition = "unknown"
        label = tool_name
        icon = (tool_name[:1] or " ").upper()
        color = "muted"
        tier = str(rules.get("default_tier", "action"))
        aggregate = rules.get("default_aggregate")
    return {
        "version": PRESENTATION_VERSION,
        "disposition": disposition,
        "tool_name": tool_name,
        "label": label,
        "icon": icon,
        "color": color,
        "tier": tier,
        "aggregate": aggregate,
        "mcp_namespace": namespace,
        "tool_input_json": tool_input_json,
        "rule_id": f"tool:{tool_name.lower()}",
    }


def project_tool_presentation(
    tool_name: str | None,
    tool_input_json: Any,
    *,
    provider: str | None = None,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> dict[str, Any] | None:
    if not tool_name:
        return None
    base = _base_projection(tool_name, tool_input_json, rules_path=rules_path)
    base.update(
        {
            "source_tool_name": tool_name,
            "execution_method": None,
            "wrapper_recedes": False,
            "children": [],
        }
    )
    is_codex = str(provider or "").lower() == "codex"
    if is_codex and tool_name.lower() == "write_stdin" and isinstance(tool_input_json, dict):
        if tool_input_json.get("chars") in {None, ""}:
            base.update({"label": "Wait", "icon": "…", "color": "tertiary", "tier": "noise", "aggregate": "wait"})
    if not is_codex or tool_name.lower() != "exec" or not isinstance(tool_input_json, str):
        return base

    calls, complete = extract_codex_wrapper_calls(tool_input_json)
    if not calls:
        base["disposition"] = "unknown"
        base["rule_id"] = "codex:exec:unparsed"
        return base
    source_digest = hashlib.sha256(tool_input_json.encode()).hexdigest()[:16]
    children = []
    for call in calls:
        child = _base_projection(call["tool_name"], call["tool_input_json"], rules_path=rules_path)
        if call["tool_name"].lower() == "write_stdin" and isinstance(call["tool_input_json"], dict):
            if call["tool_input_json"].get("chars") in {None, ""}:
                child.update({"label": "Wait", "icon": "…", "color": "tertiary", "tier": "noise", "aggregate": "wait"})
        child.update(
            {
                "child_id": f"{source_digest}:{call['source_span'][0]}:{call['source_span'][1]}",
                "source_span": call["source_span"],
                "input_complete": call["input_complete"],
                "result_forwarded": call["result_forwarded"],
            }
        )
        children.append(child)
    if len(children) == 1 and complete and children[0]["result_forwarded"]:
        child = children[0]
        return {
            **base,
            "version": PRESENTATION_VERSION,
            "disposition": "parsed",
            "tool_name": child["tool_name"],
            "label": child["label"],
            "icon": child["icon"],
            "color": child["color"],
            "tier": child["tier"],
            "aggregate": child["aggregate"],
            "mcp_namespace": child["mcp_namespace"],
            "tool_input_json": child["tool_input_json"],
            "source_tool_name": tool_name,
            "execution_method": "exec",
            "wrapper_recedes": True,
            "children": children,
            "rule_id": "codex:exec:single-child:v1",
        }
    return {
        **base,
        "disposition": "parsed",
        "label": f"Called {len(children)} tools",
        "execution_method": "exec",
        "wrapper_recedes": False,
        "children": children,
        "rule_id": "codex:exec:multi-child:v1",
    }
