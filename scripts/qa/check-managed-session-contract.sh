#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${MANAGED_SESSION_CONTRACT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

python3 - "$ROOT_DIR" <<'PY'
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


def rel(path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def require_file(relative: str) -> Path:
    path = root / relative
    if not path.exists():
        fail(f"required managed-session contract file is missing: {relative}")
    return path


def read_text(relative: str) -> str:
    path = require_file(relative)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_python(relative: str) -> ast.Module:
    text = read_text(relative)
    try:
        return ast.parse(text, filename=relative)
    except SyntaxError as exc:
        fail(f"could not parse {relative}: {exc}")
        return ast.Module(body=[], type_ignores=[])


def call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def is_true_keyword(call: ast.Call, name: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
            return True
    return False


def calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and call_name(node) == name]


def has_keyword_name(call: ast.Call, keyword_name: str, value_name: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg != keyword_name:
            continue
        value = keyword.value
        return isinstance(value, ast.Name) and value.id == value_name
    return False


def require_contains(relative: str, label: str, pattern: str) -> None:
    text = read_text(relative)
    if not re.search(pattern, text, flags=re.MULTILINE | re.DOTALL):
        fail(f"required managed-session contract reference is missing: {label} ({relative})")


for required in [
    "docs/specs/managed-provider-session-contract.md",
    "server/zerg/cli/_managed_contract.py",
    "server/zerg/services/managed_session_contracts.py",
    "server/zerg/services/local_health.py",
    "server/zerg/cli/claude.py",
    "server/zerg/cli/codex.py",
    "server/zerg/cli/opencode.py",
    "server/zerg/cli/antigravity.py",
]:
    require_file(required)

require_contains(
    "server/zerg/cli/_managed_contract.py",
    "provider homes are mapped out of provider-owned directories",
    r"resolve_longhouse_home_from_provider_home\(config_dir\)",
)
require_contains(
    "server/zerg/services/managed_session_contracts.py",
    "provider version capture must be bounded",
    r"def\s+capture_provider_version\([^)]*timeout_seconds:\s*float\s*=\s*1\.0",
)
require_contains(
    "server/zerg/services/managed_session_contracts.py",
    "stale contract removal helper",
    r"def\s+remove_managed_session_contract\(",
)
require_contains(
    "server/zerg/services/local_health.py",
    "local-health filters contracts to active managed session ids",
    r"managed_session_ids\s*=\s*\{[^}]*for\s+session\s+in\s+managed_sessions",
)
require_contains(
    "server/zerg/services/local_health.py",
    "local-health passes active session ids into contract scan",
    r"collect_managed_session_contract_diagnostics\([^)]*session_ids\s*=\s*managed_session_ids",
)

for relative in ["server/zerg/cli/claude.py", "server/zerg/cli/codex.py"]:
    tree = parse_python(relative)
    for function_name in ["record_managed_provider_contract", "remove_managed_provider_contract"]:
        found = calls_named(tree, function_name)
        if not found:
            fail(f"{relative} does not call {function_name}")
            continue
        for call in found:
            if not is_true_keyword(call, "config_dir_is_provider_home"):
                fail(
                    f"{relative}:{getattr(call, 'lineno', '?')} calls {function_name} "
                    "without config_dir_is_provider_home=True"
                )

opencode_tree = parse_python("server/zerg/cli/opencode.py")
native_opencode = next(
    (node for node in ast.walk(opencode_tree) if isinstance(node, ast.FunctionDef) and node.name == "_run_native_opencode"),
    None,
)
if native_opencode is None:
    fail("server/zerg/cli/opencode.py is missing _run_native_opencode")
else:
    record_state = next(
        (node for node in native_opencode.body if isinstance(node, ast.FunctionDef) and node.name == "_record_state"),
        None,
    )
    if record_state is None:
        fail("_run_native_opencode must define _record_state")
    else:
        bridge_writes = calls_named(record_state, "write_opencode_bridge_state")
        contract_records = calls_named(record_state, "record_managed_provider_contract")
        if not bridge_writes:
            fail("_record_state must write opencode bridge state before recording the contract")
        if not contract_records:
            fail("_record_state must record a managed-session contract")
        if bridge_writes and contract_records:
            if bridge_writes[0].lineno >= contract_records[0].lineno:
                fail("opencode contract is recorded before bridge state exists")
            if not has_keyword_name(contract_records[0], "control_state_path", "state_path"):
                fail("opencode bridge contract must persist control_state_path=state_path")

for relative in ["server/zerg/cli/opencode.py", "server/zerg/cli/antigravity.py"]:
    tree = parse_python(relative)
    if not calls_named(tree, "record_managed_provider_contract"):
        fail(f"{relative} does not record managed-session contracts")
    if not calls_named(tree, "remove_managed_provider_contract"):
        fail(f"{relative} does not remove managed-session contracts on clean exit")

for scan_root in [
    root / "server/zerg",
    root / "engine",
    root / "scripts",
    root / ".github",
]:
    if not scan_root.exists():
        continue
    for path in sorted(scan_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in {"check-managed-session-contract.sh", "managed-session-contract.test.py"}:
            continue
        if any(part in {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "generated", "node_modules", "target"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in [
            r"\.claude/managed-local/contracts",
            r"\.codex/managed-local/contracts",
            r"\.gemini/managed-local/contracts",
        ]:
            match = re.search(pattern, text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                fail(f"{rel(path)}:{line} references provider-owned managed-session contract storage")

temp_mark = "longhouse-managed-session-temp-cwd-ok"
provider_command = re.compile(r"longhouse\s+(claude|codex|opencode|antigravity)\b")
temp_token = re.compile(r"\b(TMP|TEMP|mktemp|TemporaryDirectory|/tmp)\b")
cleanup_token = re.compile(r"\b(rm\s+-rf|trap\b.*rm\s+-rf|cleanup)\b")
for scan_root in [root / "scripts/qa", root / "scripts/tests", root / ".github/workflows"]:
    if not scan_root.exists():
        continue
    for path in sorted(scan_root.rglob("*")):
        if not path.is_file() or path.name == "managed-session-contract.test.py":
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for index, line in enumerate(lines):
            if not provider_command.search(line):
                continue
            window = "\n".join(lines[max(0, index - 20) : min(len(lines), index + 21)])
            command_window = "\n".join(lines[index : min(len(lines), index + 8)])
            if "--cwd" not in command_window:
                continue
            if temp_token.search(window) and cleanup_token.search(window) and temp_mark not in window:
                fail(
                    f"{rel(path)}:{index + 1} launches a managed provider from a temp cwd with cleanup; "
                    f"add {temp_mark} only after the session is stopped before cwd cleanup"
                )

if errors:
    for error in errors:
        print(error, file=sys.stderr)
    print("managed-session contract check failed", file=sys.stderr)
    raise SystemExit(1)

print("managed-session contract check passed")
PY
