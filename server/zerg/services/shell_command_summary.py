"""Bounded, fail-closed shell source summaries for timeline presentation.

This module describes literal shell source. It never executes commands, expands
variables, resolves PATH, or claims that a process actually ran.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

PARSER_ID = "bounded-shell-v1"
SHAPE_REGISTRY_VERSION = 1
MAX_SOURCE_CHARS = 12_000
MAX_TOKENS = 512
MAX_DEPTH = 3
MAX_CANDIDATES = 8

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_SHAPE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$")
_SECRET_WORD = re.compile(r"(?:token|secret|password|passwd|authorization|credential|api[_-]?key)", re.IGNORECASE)
_DYNAMIC_WORD = re.compile(r"[$`*?{}\[\]]")
_LIST_OPERATORS = {";", "&&", "||", "&"}
_PIPE_OPERATORS = {"|", "|&"}
_REDIRECT_OPERATORS = {"<", ">", ">>", "<<", "<<<", ">&", "<&", "<>"}
_CONTROL_WORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "for",
    "while",
    "until",
    "do",
    "done",
    "in",
    "case",
    "esac",
    "function",
    "select",
}
_OPAQUE_COMMANDS = {"eval", "source", "."}
_STATEFUL_COMMANDS = {"alias", "unalias"}


@dataclass(frozen=True)
class _Operation:
    label: str
    executable: str
    subcommands: tuple[str, ...]

    @property
    def key(self) -> str:
        return self.label


@dataclass
class _ScanResult:
    operations: list[_Operation]
    partial: bool = False
    dynamic: bool = False
    truncated: bool = False
    parse_error: str | None = None


def _normalize_unquoted_newlines(source: str) -> str:
    out: list[str] = []
    single = False
    double = False
    escaped = False
    for char in source:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\" and not single:
            out.append(char)
            escaped = True
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        if char in "\r\n" and not single and not double:
            out.append(";")
        else:
            out.append(char)
    return "".join(out)


def _tokenize(source: str) -> tuple[list[str], str | None]:
    try:
        lexer = shlex.shlex(
            _normalize_unquoted_newlines(source),
            posix=True,
            punctuation_chars=";&|()<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return [], "unbalanced_quotes"
    if len(tokens) > MAX_TOKENS:
        return tokens[:MAX_TOKENS], "token_limit"
    return tokens, None


def _is_assignment(word: str) -> bool:
    return bool(_ASSIGNMENT.match(word))


def _safe_shape_token(word: str) -> str | None:
    if not _SAFE_SHAPE_TOKEN.fullmatch(word) or _SECRET_WORD.search(word):
        return None
    return word


def _basename(word: str) -> str | None:
    if not word or _DYNAMIC_WORD.search(word):
        return None
    value = os.path.basename(word.rstrip("/"))
    return _safe_shape_token(value)


def _skip_options(args: list[str], options_with_values: set[str]) -> list[str]:
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            return args[index + 1 :]
        if not word.startswith("-") or word == "-":
            return args[index:]
        option = word.split("=", 1)[0]
        if option in options_with_values and "=" not in word:
            index += 2
        else:
            index += 1
    return []


def _unwrap(tokens: list[str], *, depth: int) -> tuple[list[str], bool, _ScanResult | None]:
    """Return the literal wrapped command, whether nesting made it partial."""

    current = tokens
    partial = False
    for _ in range(8):
        if not current:
            return [], partial, None
        head = _basename(current[0])
        if head == "env":
            args = _skip_options(current[1:], {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
            while args and _is_assignment(args[0]):
                args = args[1:]
            current = args
            continue
        if head in {"sudo", "doas"}:
            current = _skip_options(
                current[1:],
                {
                    "-u",
                    "--user",
                    "-g",
                    "--group",
                    "-h",
                    "--host",
                    "-C",
                    "--close-from",
                    "-p",
                    "--prompt",
                    "-R",
                    "--chroot",
                    "-T",
                    "--command-timeout",
                },
            )
            continue
        if head == "timeout":
            args = _skip_options(current[1:], {"-k", "--kill-after", "-s", "--signal"})
            if not args:
                return [], True, None
            current = args[1:]  # literal duration
            continue
        if head == "command":
            args = _skip_options(current[1:], set())
            if len(args) != len(current) - 1 and any(value in current[1:] for value in {"-v", "-V"}):
                return [], True, None
            current = args
            continue
        if head == "nice":
            current = _skip_options(current[1:], {"-n", "--adjustment"})
            continue
        if head == "nohup":
            current = current[1:]
            continue
        if head in {"sh", "bash", "zsh"}:
            args = current[1:]
            script_index = next((i + 1 for i, value in enumerate(args) if value in {"-c", "-lc", "-cl"}), None)
            if script_index is None or script_index >= len(args):
                return current, partial, None
            if depth >= MAX_DEPTH:
                return [], True, _ScanResult([], partial=True, dynamic=True, truncated=True, parse_error="depth_limit")
            nested = _scan(args[script_index], depth=depth + 1)
            nested.partial = True
            return [], True, nested
        break
    return current, partial, None


def _shape_operation(tokens: list[str], *, depth: int) -> tuple[_Operation | None, bool, _ScanResult | None]:
    while tokens and _is_assignment(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None, False, None

    unwrapped, partial, nested = _unwrap(tokens, depth=depth)
    if nested is not None:
        return None, True, nested
    tokens = unwrapped
    while tokens and _is_assignment(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None, partial, None

    executable = _basename(tokens[0])
    if executable is None or executable in _OPAQUE_COMMANDS or executable in _CONTROL_WORDS:
        return None, True, _ScanResult([], partial=True, dynamic=True)
    if executable in {"cd", "pushd", "popd"}:
        return None, partial, None

    args = tokens[1:]
    subcommands: list[str] = []
    if executable == "gh":
        args = _skip_options(args, {"-R", "--repo", "--hostname"})
        if args:
            first = _safe_shape_token(args[0])
            if first:
                subcommands.append(first)
                if first in {"run", "pr", "workflow", "issue", "release", "repo"} and len(args) > 1:
                    second = _safe_shape_token(args[1])
                    if second and not second.startswith("-"):
                        subcommands.append(second)
    elif executable == "git":
        args = _skip_options(args, {"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
        if args and (value := _safe_shape_token(args[0])):
            subcommands.append(value)
    elif executable == "docker":
        args = _skip_options(args, {"-H", "--host", "--context", "--config", "--log-level"})
        if args and (first := _safe_shape_token(args[0])):
            subcommands.append(first)
            if first == "compose" and len(args) > 1 and (second := _safe_shape_token(args[1])):
                subcommands.append(second)
    elif executable == "kubectl":
        args = _skip_options(args, {"--context", "--namespace", "-n", "--kubeconfig", "--cluster", "--user"})
        if args and (value := _safe_shape_token(args[0])):
            subcommands.append(value)
    elif executable in {"npm", "pnpm", "yarn", "bun"}:
        args = _skip_options(args, {"--cwd", "-C", "--dir"})
        if args and (first := _safe_shape_token(args[0])):
            subcommands.append(first)
            if first == "run" and len(args) > 1 and (script := _safe_shape_token(args[1])):
                subcommands.append(script)
    elif executable == "uv":
        args = _skip_options(args, {"--directory", "--project", "--python"})
        if args and (first := _safe_shape_token(args[0])):
            subcommands.append(first)
            if first == "run" and len(args) > 1 and (command := _basename(args[1])):
                subcommands.append(command)
    elif executable == "make":
        args = _skip_options(args, {"-C", "--directory", "-f", "--file", "--makefile"})
        if args and (target := _safe_shape_token(args[0])):
            subcommands.append(target)
    elif executable in {"cargo", "go"}:
        args = _skip_options(args, set())
        if args and (value := _safe_shape_token(args[0])):
            subcommands.append(value)

    label = " ".join([executable, *subcommands])
    return _Operation(label=label, executable=executable, subcommands=tuple(subcommands)), partial, None


def _strip_redirections(tokens: list[str]) -> tuple[list[str], bool]:
    clean: list[str] = []
    index = 0
    redirected = False
    while index < len(tokens):
        token = tokens[index]
        if (
            token.isdigit()
            and index + 1 < len(tokens)
            and (tokens[index + 1] in _REDIRECT_OPERATORS or re.fullmatch(r"\d*(?:>>?|<<?|>&|<&)", tokens[index + 1]))
        ):
            redirected = True
            index += 3
            continue
        if token in _REDIRECT_OPERATORS or re.fullmatch(r"\d*(?:>>?|<<?|>&|<&)", token):
            redirected = True
            index += 2
            continue
        clean.append(token)
        index += 1
    return clean, redirected


def _scan(source: str, *, depth: int = 0) -> _ScanResult:
    tokens, error = _tokenize(source)
    if not tokens:
        return _ScanResult([], parse_error=error or "empty")

    # Function declarations and subshell/group syntax are intentionally outside
    # this scanner's grammar. Mining commands from their bodies would confuse
    # declared source with commands the shell was asked to evaluate now.
    if any("(" in token or ")" in token for token in tokens) or tokens[0] == "function":
        return _ScanResult([], partial=True, dynamic=True, parse_error="unsupported_group")

    # Control flow and alias mutation change whether later source is reached or
    # what its command word resolves to. A syntactic headline cannot be honest
    # without evaluating that state, so the complete source stays opaque.
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _LIST_OPERATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        segment, _ = _strip_redirections(segment)
        while segment and _is_assignment(segment[0]):
            segment = segment[1:]
        head = _basename(segment[0]) if segment else None
        if head in _CONTROL_WORDS:
            return _ScanResult([], partial=True, dynamic=True, parse_error="unsupported_control")
        if head in _STATEFUL_COMMANDS:
            return _ScanResult([], partial=True, dynamic=True, parse_error="unsupported_alias")

    heredoc = False
    if any(token in {"<<", "<<<"} for token in tokens):
        heredoc = True
        operator_index = next(index for index, token in enumerate(tokens) if token in {"<<", "<<<"})
        boundary = next(
            (index for index in range(operator_index + 1, len(tokens)) if tokens[index] in _LIST_OPERATORS),
            len(tokens),
        )
        tokens = tokens[:boundary]

    result = _ScanResult(
        [],
        partial=heredoc,
        dynamic=heredoc,
        truncated=error == "token_limit",
        parse_error=error,
    )
    segment: list[str] = []
    index = 0

    def flush(raw: list[str]) -> None:
        if not raw or len(result.operations) >= MAX_CANDIDATES:
            if raw:
                result.truncated = True
                result.partial = True
            return
        primary = raw
        for pipe_index, token in enumerate(raw):
            if token in _PIPE_OPERATORS:
                primary = raw[:pipe_index]
                result.partial = True
                break
        primary, redirected = _strip_redirections(primary)
        result.partial = result.partial or redirected
        operation, partial, nested = _shape_operation(primary, depth=depth)
        result.partial = result.partial or partial
        if nested is not None:
            result.operations.extend(nested.operations[: max(0, MAX_CANDIDATES - len(result.operations))])
            result.partial = result.partial or nested.partial
            result.dynamic = result.dynamic or nested.dynamic
            result.truncated = result.truncated or nested.truncated
            result.parse_error = result.parse_error or nested.parse_error
        elif operation is not None:
            result.operations.append(operation)
        elif primary:
            result.dynamic = True

    while index < len(tokens):
        token = tokens[index]
        if token in _LIST_OPERATORS:
            flush(segment)
            segment = []
            result.partial = result.partial or bool(result.operations)
        elif token in {"(", ")"}:
            result.partial = True
            result.dynamic = True
        else:
            segment.append(token)
        index += 1
    flush(segment)
    return result


def summarize_shell_source(source: Any) -> dict[str, Any] | None:
    """Return a JSON-safe disposable presentation summary for literal source."""

    if not isinstance(source, str) or not source.strip():
        return None
    if len(source) > MAX_SOURCE_CHARS:
        scan = _scan(source[:MAX_SOURCE_CHARS])
        scan.truncated = True
        scan.partial = True
        scan.parse_error = "source_limit"
    else:
        scan = _scan(source)

    ordered: list[_Operation] = []
    counts: dict[str, int] = {}
    for operation in scan.operations:
        counts[operation.key] = counts.get(operation.key, 0) + 1
        if counts[operation.key] == 1:
            ordered.append(operation)

    confidence = (
        "opaque" if not ordered else "partial" if scan.partial or scan.dynamic or scan.truncated or scan.parse_error else "syntactic"
    )
    return {
        "version": 1,
        "confidence": confidence,
        "operations": [
            {
                "key": operation.key,
                "label": operation.label,
                "executable": operation.executable,
                "subcommands": list(operation.subcommands),
                "count": counts[operation.key],
            }
            for operation in ordered
        ],
        "candidate_count": len(ordered),
        "truncated": scan.truncated,
        "dynamic": scan.dynamic,
        "parse_error": scan.parse_error,
        "parser_id": PARSER_ID,
        "shape_registry_version": SHAPE_REGISTRY_VERSION,
    }
