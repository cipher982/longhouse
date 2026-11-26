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
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from zerg.services.shell_command_summary import summarize_shell_source

PRESENTATION_VERSION = 2
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
    # Lookup is exact name first, then case-insensitive, on every surface.
    value["_tools_by_lower"] = {str(name).lower(): meta for name, meta in value["tools"].items()}
    return value


def _lookup_tool(rules: Mapping[str, Any], tool_name: str) -> Mapping[str, Any] | None:
    exact = rules["tools"].get(tool_name)
    if exact is not None:
        return exact
    return rules["_tools_by_lower"].get(tool_name.lower())


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

    resolved: tuple[Any, bool] = (identifier, False)
    index = 0
    while index < before:
        char = source[index]
        if char in {'"', "'", "`"}:
            end = _scan_quoted(source, index)
            if end is None:
                return identifier, False
            index = end
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2, before)
            index = before if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2, before)
            if end < 0:
                return identifier, False
            index = end + 2
            continue
        identifier_match = _IDENTIFIER.match(source, index)
        if identifier_match is not None and identifier_match.group(0) == identifier:
            previous = source[index - 1] if index > 0 else ""
            if not (previous.isalnum() or previous in "_$."):
                operator_start = _skip_space(source, identifier_match.end())
                operator_match = re.match(r"(?:\?\?=|&&=|\|\|=|[+\-*/%&|^]?=)", source[operator_start:before])
                if operator_match and not source.startswith(("==", "=>"), operator_start):
                    resolved = (identifier, False)
                    index = operator_start + len(operator_match.group(0))
                    continue
        keyword = next((value for value in ("const", "let", "var") if source.startswith(value, index)), None)
        if keyword is None or (index > 0 and (source[index - 1].isalnum() or source[index - 1] in "_$")):
            index += 1
            continue
        name_start = _skip_space(source, index + len(keyword))
        match = _IDENTIFIER.match(source, name_start)
        if match is None or match.group(0) != identifier:
            index += len(keyword)
            continue
        equals = _skip_space(source, match.end())
        if equals >= before or source[equals] != "=":
            resolved = (identifier, False)
            index = match.end()
            continue
        value_start = _skip_space(source, equals + 1)
        if value_start >= before or source[value_start] not in {'"', "'", "`"}:
            resolved = (identifier, False)
            index = value_start
            continue
        value_end = _scan_quoted(source, value_start)
        if value_end is None or value_end > before:
            return identifier, False
        resolved = _parse_js_literal(source[value_start:value_end])
        index = value_end
    return resolved


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
    exact = _lookup_tool(rules, tool_name)
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


def _shell_command(tool_input: Mapping[str, Any]) -> str | None:
    """The command text under any provider's key (Antigravity run_command uses ``CommandLine``)."""
    for key in ("command", "cmd", "CommandLine"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
        # Codex `shell` sends argv (`["bash", "-lc", "ls -la"]`): the script is the last word.
        if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
            if len(value) >= 3 and value[-2] in {"-c", "-lc"}:
                return value[-1]
            return " ".join(value)
    return None


def _attach_shell_summary(projection: dict[str, Any], *, rules_path: Path) -> dict[str, Any]:
    """Attach a disposable shell summary to the final presented tool only."""

    rules = _load_rules(str(rules_path.resolve()))
    configured = {str(name).lower() for name in rules.get("shell_classifier", {}).get("shell_tools", [])}
    if str(projection.get("tool_name") or "").lower() not in configured:
        return projection
    tool_input = projection.get("tool_input_json")
    if not isinstance(tool_input, dict):
        return projection
    source = _shell_command(tool_input)
    summary = summarize_shell_source(source)
    if summary is not None:
        projection["shell_summary"] = summary
    return projection


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
    if str(provider or "").lower() == "cursor" and tool_name == "Task" and isinstance(tool_input_json, dict):
        agent_type = tool_input_json.get("subagent_type")
        if isinstance(agent_type, str) and agent_type.strip():
            base.update(
                {
                    "label": "Agent",
                    "icon": "A",
                    "color": "tertiary",
                    "rule_id": "cursor:task:delegation",
                }
            )
    is_codex = str(provider or "").lower() == "codex"
    if is_codex and tool_name.lower() == "write_stdin" and isinstance(tool_input_json, dict):
        if tool_input_json.get("chars") in {None, ""}:
            base.update({"label": "Wait", "icon": "…", "color": "tertiary", "tier": "noise", "aggregate": "wait"})
    if not is_codex or tool_name.lower() != "exec" or not isinstance(tool_input_json, str):
        return _attach_shell_summary(base, rules_path=rules_path)

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
        promoted = {
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
        return _attach_shell_summary(promoted, rules_path=rules_path)
    return {
        **base,
        "disposition": "parsed",
        "label": f"Called {len(children)} tools",
        "execution_method": "exec",
        "wrapper_recedes": False,
        "children": children,
        "rule_id": "codex:exec:multi-child:v1",
    }


# Tools whose ``description`` argument states why the call was made. Mirrors the
# engine's DESCRIPTION_IS_INTENT (engine/src/pipeline/items.rs) plus Claude
# ``Monitor``, whose description is its purpose on ~96% of calls. On any other
# tool (an MCP issue tracker, Claude ``Artifact``) ``description`` is payload.
DESCRIPTION_IS_INTENT = frozenset(
    {
        "bash",
        "shell",
        "shell_command",
        "exec_command",
        "run_shell_command",
        "run_terminal_cmd",
        "task",
        "agent",
        "monitor",
    }
)
# Tools whose ``reason`` / ``summary`` argument is the model's own one-line purpose.
_REASON_IS_INTENT = frozenset({"enter_plan_mode", "enterplanmode", "schedulewakeup"})
_SUMMARY_IS_INTENT = frozenset({"sendmessage"})
_INTENT_CHARS = 160


def _compact(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split()).strip()
    return compact[:_INTENT_CHARS] if compact else None


def _input_mapping(tool_input_json: Any) -> Mapping[str, Any] | None:
    parsed: Any = tool_input_json
    if isinstance(parsed, str):
        stripped = parsed.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except Exception:
            return None
    return parsed if isinstance(parsed, Mapping) else None


def extract_authored_tool_intent(tool_name: str | None, tool_input_json: Any) -> str | None:
    """The purpose the agent itself wrote into the call's arguments, if any.

    OMP/Pi ``i`` (OMP ``eval`` uses ``title``), Antigravity ``toolSummary``, and
    ``description`` only on tools where it is the purpose (Claude/Cursor/OpenCode
    shell and subagent calls). An MCP tool's arguments are its payload, never
    its intent.
    """
    name = str(tool_name or "").strip()
    if not name or _mcp_parts(name) is not None:
        return None
    parsed = _input_mapping(tool_input_json)
    if parsed is None:
        return None
    lowered = name.lower()
    candidates: list[object] = [parsed.get("i"), parsed.get("toolSummary")]
    if lowered == "eval":
        candidates.append(parsed.get("title"))
    if lowered in DESCRIPTION_IS_INTENT:
        candidates.append(parsed.get("description"))
    if lowered in _REASON_IS_INTENT:
        candidates.append(parsed.get("reason"))
    if lowered in _SUMMARY_IS_INTENT:
        candidates.append(parsed.get("summary"))
    for value in candidates:
        compact = _compact(value)
        if compact:
            return compact
    return None


def _path_tail(path: str) -> str:
    parts = [p for p in path.strip().replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else path.strip()[:_INTENT_CHARS])


_PATH_KEYS = (
    "file_path",
    "path",
    "filePath",
    "Path",
    "AbsolutePath",
    "DirectoryPath",
    "target_file",
    "notebook_path",
)
_PATTERN_KEYS = ("pattern", "query", "Query", "glob", "glob_pattern", "Pattern")
_WEB_KEYS = ("query", "url", "Url", "Query")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
_TARGET_CHARS = 2000


def _first_text(parsed: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _path_list(parsed: Mapping[str, Any]) -> list[str]:
    """Several files in one call (``paths``, ``file_paths``)."""
    for key in ("paths", "file_paths"):
        value = parsed.get(key)
        if isinstance(value, list):
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _files_summary(paths: list[str]) -> str:
    unique = list(dict.fromkeys(paths))
    extra = len(unique) - 1
    first = _path_tail(unique[0])
    return first if extra <= 0 else f"{first} + {extra} {'file' if extra == 1 else 'files'}"


def _first_line(text: str) -> str | None:
    for line in text.strip().splitlines():
        compact = " ".join(line.split())
        if compact:
            return compact
    return None


def _patch_source(tool_input_json: Any, parsed: Mapping[str, Any] | None) -> str | None:
    """Patch text from Codex apply_patch, stored raw or under ``patch``/``input``."""
    if parsed is not None:
        for key in ("patch", "input"):
            value = parsed.get(key)
            if isinstance(value, str) and "*** Begin Patch" in value:
                return value
        return None
    if isinstance(tool_input_json, str) and "*** Begin Patch" in tool_input_json:
        return tool_input_json
    return None


def _patch_summary(patch: str) -> str:
    paths = [m.strip() for m in _PATCH_FILE.findall(patch)]
    return _files_summary(paths) if paths else "Applied patch"


def _swarm_summary(name: str, parsed: Mapping[str, Any]) -> str | None:
    """OMP swarm/board tools: the action and its subject, not the JSON."""

    def text(key: str) -> str:
        value = parsed.get(key)
        if not isinstance(value, str):
            return ""
        value = value.strip()
        # Compact OMP args arrive JSON-quoted ('"add"').
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                decoded = json.loads(value)
            except ValueError:
                return value
            return decoded.strip() if isinstance(decoded, str) else value
        return value

    if name == "swarm":
        action = text("action").lower()
        worker = text("name")
        tasks = parsed.get("tasks")
        count = len(tasks) if isinstance(tasks, list) else (parsed.get("n") if isinstance(parsed.get("n"), int) else 0)
        if action == "add":
            workspace = text("workspace")
            suffix = f" x {count}" if count and count > 1 else ""
            where = f" ({workspace})" if workspace and workspace != "inherit" else ""
            return f"add {worker or 'worker'}{suffix}{where}"
        if action == "kill" and worker:
            return f"kill {worker}"
        if action == "title" and text("title"):
            return f"title: {text('title')}"
        if action == "model" and text("model"):
            return f"model {worker} -> {text('model')}" if worker else f"model {text('model')}"
        return action or None
    if name == "post_message":
        return _first_line(text("text"))
    if name == "read_board":
        since = parsed.get("since")
        return f"since #{since}" if isinstance(since, (int, str)) and str(since).strip() else "read board"
    if name == "swarm_note":
        for key in ("title", "goal", "text", "note"):
            line = _first_line(text(key))
            if line:
                return line
    return None


@lru_cache(maxsize=4096)
def _label_for_cached(name: str, rules_key: str) -> str | None:
    meta = _lookup_tool(_load_rules(rules_key), name)
    return str(meta.get("label")) if meta is not None else None


def _label_for(name: str, rules_path: Path) -> str | None:
    rules_key = str(DEFAULT_RULES_PATH.resolve()) if rules_path is DEFAULT_RULES_PATH else str(rules_path.resolve())
    return _label_for_cached(name, rules_key)


_SHELL_WORD = r"""(?:"[^"]*"|'[^']*'|[^\s;&|'"])+"""
# Leading setup that says where or with what, not what: `cd <dir> &&`,
# `cd <dir>;`, `VAR=value;`, `export VAR=value &&`, or either on its own line.
# A bare `VAR=value cmd` (an environment prefix) stays: it belongs to the
# command.
_SHELL_SETUP = re.compile(
    rf"""^[ \t]*(?:cd[ \t]+{_SHELL_WORD}|(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=(?:{_SHELL_WORD})?)"""
    r"""[ \t]*(?:&&|;|\n)(?:[ \t]|\\\n|\n)*"""
)


def _shell_headline(command: str) -> str | None:
    """A command's first line without its leading cd and variable setup.

    `cd /tmp/x/scratchpad && python3 - <<'EOF'` reads as `python3 - <<'EOF'`.
    When what is left is not a command (a comment, a stray continuation),
    the plain first line stands. The full command stays the call's target.
    """
    first = _first_line(command)
    current = command
    while True:
        match = _SHELL_SETUP.match(current)
        if not match:
            break
        current = current[match.end() :]
    line = _first_line(current)
    if not line or line.startswith("#") or line.rstrip("\\ \t") == "":
        return first
    return line.rstrip(" \t\\").strip() or first


def extract_derived_tool_intent(
    tool_name: str | None,
    tool_input_json: Any,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> str | None:
    """A one-line label Longhouse derives from the call's arguments.

    Chosen by the tool's canonical label in config/tool-tiers.json, so every
    provider's spelling of a read, search or shell call gets the same rule: a
    path tail, a pattern, a query or URL, the first line of a command, the
    files a patch touches, or the first line of a subagent prompt. A tool with
    no rule falls back to the first familiar argument. This is what the call
    does, not why. Clients render it as given and never re-derive it.
    """
    name = str(tool_name or "").strip()
    if not name:
        return None
    parsed = _input_mapping(tool_input_json)
    patch = _patch_source(tool_input_json, parsed)
    if patch is not None:
        return _patch_summary(patch)
    if parsed is None:
        return None
    label = _label_for(name, rules_path)
    lowered = name.lower()

    if label in {"Read", "Edit", "Write", "Notebook"}:
        path = _first_text(parsed, _PATH_KEYS)
        if path:
            return _path_tail(path)
        paths = _path_list(parsed)
        return _files_summary(paths) if paths else None
    if label in {"Search", "List"}:
        pattern = _first_text(parsed, _PATTERN_KEYS)
        if pattern:
            return _compact(pattern)
        path = _first_text(parsed, _PATH_KEYS)
        return _path_tail(path) if path else None
    if label in {"Fetch", "Web search"}:
        return _compact(_first_text(parsed, _WEB_KEYS))
    if label == "Shell":
        cmd = _shell_command(parsed)
        line = _shell_headline(cmd) if isinstance(cmd, str) else None
        return line[:_INTENT_CHARS] if line else None
    swarm = _swarm_summary(lowered, parsed)
    if swarm:
        return _compact(swarm)
    if label == "Agent" or lowered == "subagent":
        prompt = _first_text(parsed, ("prompt", "Prompt", "message", "task"))
        line = _first_line(prompt) if prompt else None
        return line[:_INTENT_CHARS] if line else None
    if label in {"Plan", "Wait", "Ask"}:
        return None
    # No canonical rule: the first argument a reader would recognize.
    path = _first_text(parsed, _PATH_KEYS)
    if path:
        return _path_tail(path)
    paths = _path_list(parsed)
    if paths:
        return _files_summary(paths)
    for key in ("command", "cmd", "pattern", "query", "url", "prompt", "key"):
        value = parsed.get(key)
        if isinstance(value, str):
            line = _first_line(value)
            if line:
                return line[:_INTENT_CHARS]
    return None


def extract_tool_target(
    tool_name: str | None,
    tool_input_json: Any,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> str | None:
    """The literal machine target of a call: the full command, path, pattern or URL.

    Shown on hover or in a mono line under an authored purpose, so a reader
    can always see exactly what ran. Never a summary.
    """
    name = str(tool_name or "").strip()
    if not name:
        return None
    parsed = _input_mapping(tool_input_json)
    patch = _patch_source(tool_input_json, parsed)
    if patch is not None:
        paths = list(dict.fromkeys(m.strip() for m in _PATCH_FILE.findall(patch)))
        return ", ".join(paths)[:_TARGET_CHARS] or None
    if parsed is None:
        return None
    label = _label_for(name, rules_path)
    if label == "Shell":
        cmd = _shell_command(parsed)
        return cmd.strip()[:_TARGET_CHARS] if isinstance(cmd, str) and cmd.strip() else None
    order: tuple[str, ...]
    if label in {"Search", "List"}:
        order = (*_PATTERN_KEYS, *_PATH_KEYS)
    elif label in {"Fetch", "Web search"}:
        order = ("url", "Url", "query", "Query")
    else:
        order = (*_PATH_KEYS, "command", "cmd", "CommandLine", "pattern", "url", "query")
    value = _first_text(parsed, order) or ", ".join(_path_list(parsed))
    return value[:_TARGET_CHARS] if value else None


def resolve_tool_intent(
    tool_name: str | None,
    tool_input_json: Any,
    *,
    provider_intents: tuple[str | None, ...] = (),
    rules_path: Path = DEFAULT_RULES_PATH,
) -> tuple[str | None, str | None]:
    """``(tool_intent, tool_intent_source)`` for one call.

    Provider-authored purpose always wins over text Longhouse derives from the
    arguments. ``provider_intents`` are intents the engine already extracted
    from the provider record (block-level OMP ``intent``, guarded
    ``description``, Antigravity ``toolSummary``), in order of preference.
    Source is ``"authored"`` or ``"derived"``, and None when there is no intent.
    """
    if not tool_name:
        return None, None
    for value in provider_intents:
        compact = _compact(value)
        if compact:
            return compact, "authored"
    authored = extract_authored_tool_intent(tool_name, tool_input_json)
    if authored:
        return authored, "authored"
    derived = extract_derived_tool_intent(tool_name, tool_input_json, rules_path=rules_path)
    if derived:
        return derived, "derived"
    return None, None


def resolve_call_text(
    presentation: Mapping[str, Any] | None,
    *,
    provider_intents: tuple[str | None, ...] = (),
    rules_path: Path = DEFAULT_RULES_PATH,
) -> dict[str, str | None]:
    """The served text of one call: ``tool_intent``, ``tool_intent_source``, ``tool_target``.

    Reads the presentation's effective tool, so a Codex ``exec`` wrapper that
    forwards one child is described by that child (the raw wrapper is
    JavaScript and has no arguments to read). A wrapper with several children
    is described by their labels.
    """
    empty: dict[str, str | None] = {"tool_intent": None, "tool_intent_source": None, "tool_target": None}
    if not presentation:
        return empty
    name = str(presentation.get("tool_name") or "")
    tool_input = presentation.get("tool_input_json")
    children = presentation.get("children") or []
    if children and not presentation.get("wrapper_recedes"):
        parts: list[str] = []
        targets: list[str] = []
        for child in children:
            child_name = str(child.get("tool_name") or "")
            child_input = child.get("tool_input_json")
            text, _ = resolve_tool_intent(child_name, child_input, rules_path=rules_path)
            parts.append(text or str(child.get("label") or child_name or "tool"))
            target = extract_tool_target(child_name, child_input, rules_path=rules_path)
            if target:
                targets.append(target)
        intent, source = resolve_tool_intent(name, None, provider_intents=provider_intents, rules_path=rules_path)
        return {
            "tool_intent": intent or _compact(" · ".join(parts)),
            "tool_intent_source": source or "derived",
            "tool_target": "\n".join(targets)[:_TARGET_CHARS] or None,
        }
    intent, source = resolve_tool_intent(name, tool_input, provider_intents=provider_intents, rules_path=rules_path)
    return {
        "tool_intent": intent,
        "tool_intent_source": source,
        "tool_target": extract_tool_target(name, tool_input, rules_path=rules_path),
    }


def call_text_for(tool_name: str | None, tool_input_json: Any, *, provider: str | None = None) -> dict[str, str | None]:
    """Served call text for a call that has no engine facts (a live preview)."""
    return resolve_call_text(project_tool_presentation(tool_name, tool_input_json, provider=provider))


_STATUS_NAME_CHARS = 32


def _split_activity_tool(tool: str | None) -> tuple[str, bool] | None:
    """``(tool name, is_command_line)`` for a raw activity tool value.

    Some producers put a whole command line in the tool slot (Antigravity ACP
    sends ``CommandLine``, or ``name {json args}``). A tool name never contains
    whitespace, so words after the first token that are not JSON arguments
    mean the value is a command line.
    """
    raw = str(tool or "").strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    name = re.split(r"[({\[]", parts[0], maxsplit=1)[0] or parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        return name, False
    try:
        args = json.loads(rest)
    except ValueError:
        args = None
    # Only `name {json}` / `name [json]` is a tool with arguments; `sleep 6` and
    # `echo {a,b}` are command lines.
    return name, not isinstance(args, (dict, list))


def tool_status_phrase(tool: str | None, *, rules_path: Path = DEFAULT_RULES_PATH) -> str | None:
    """Present-tense status-line phrase for the tool a session is running.

    A known tool gets its canonical ``status`` ("Running a command"); an MCP
    tool ``mcp__ns__method`` gets "Using method"; anything else "Using <name>",
    with the name cut to its first token and 32 characters; a command line
    (see ``_split_activity_tool``) is "Running a command".
    """
    split = _split_activity_tool(tool)
    if split is None:
        return None
    name, is_command_line = split
    rules = _load_rules(str(rules_path.resolve()))
    if is_command_line:
        return str(rules.get("labels", {}).get("Shell") or "Running a command")
    meta = _lookup_tool(rules, name)
    if meta is not None and meta.get("status"):
        return str(meta["status"])
    mcp = _mcp_parts(name)
    if mcp is not None:
        method = mcp[1].replace("_", " ").strip() or mcp[1]
        return f"Using {method[:_STATUS_NAME_CHARS]}"
    return f"Using {name[:_STATUS_NAME_CHARS]}"


def tool_display_label(tool: str | None, *, rules_path: Path = DEFAULT_RULES_PATH) -> str | None:
    """Canonical short label (Shell, Read, ...) for a raw tool name, MCP method, or sanitized name."""
    split = _split_activity_tool(tool)
    if split is None:
        return None
    name, is_command_line = split
    if is_command_line:
        return "Shell"
    meta = _lookup_tool(_load_rules(str(rules_path.resolve())), name)
    if meta is not None:
        return str(meta["label"])
    mcp = _mcp_parts(name)
    if mcp is not None:
        return (mcp[1].replace("_", " ").strip() or mcp[1])[:_STATUS_NAME_CHARS]
    return name[:_STATUS_NAME_CHARS]
