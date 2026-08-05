#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${MANAGED_SESSION_CONTRACT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

python3 - "$ROOT_DIR" <<'PY'
from __future__ import annotations

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


def read_required(relative: str) -> str:
    path = root / relative
    if not path.is_file():
        fail(f"required managed-session contract file is missing: {relative}")
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def require_contains(relative: str, label: str, pattern: str) -> None:
    text = read_required(relative)
    if text and not re.search(pattern, text, flags=re.MULTILINE | re.DOTALL):
        fail(f"required managed-session contract reference is missing: {label} ({relative})")


# The shipped launchers are native. Guard their shared transaction seam rather
# than the removed Python entrypoints that no installed Longhouse binary used.
require_contains(
    "engine/src/managed_launch_lifecycle.rs",
    "managed launch transaction confirms readiness",
    r"pub\s+fn\s+confirm(?:_in_background)?\(",
)
require_contains(
    "engine/src/managed_launch_lifecycle.rs",
    "unconfirmed launch transactions abort on drop",
    r"impl\s+Drop\s+for\s+ManagedLaunchTransaction",
)
require_contains(
    "engine/src/longhouse.rs",
    "native Claude/Codex/OpenCode use shared registration",
    r"register_managed_launch",
)
require_contains(
    "engine/src/longhouse.rs",
    "native provider launchers use the lifecycle transaction",
    r"ManagedLaunchTransaction",
)
require_contains(
    "engine/src/cursor_helm_launcher.rs",
    "native Cursor uses shared registration",
    r"(?:managed_launch_lifecycle::)?register_managed_launch(?:_with_timeout)?\s*\(",
)
require_contains(
    "engine/src/cursor_helm_launcher.rs",
    "native Cursor uses the lifecycle transaction",
    r"managed_launch_lifecycle::ManagedLaunchTransaction",
)

# Runtime diagnostics remain server-side and must stay bounded and scoped to
# active managed sessions.
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
    "server/zerg/services/local_health/__init__.py",
    "local-health filters contracts to active managed session ids",
    r"managed_session_ids\s*=\s*\{[^}]*for\s+session\s+in\s+managed_sessions",
)
require_contains(
    "server/zerg/services/local_health/__init__.py",
    "local-health passes active session ids into contract scan",
    r"collect_managed_session_contract_diagnostics\([^)]*session_ids\s*=\s*managed_session_ids",
)

# Longhouse-owned contracts must never move back under a provider-owned home.
for scan_root in [root / "server/zerg", root / "engine", root / "scripts", root / ".github"]:
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

# QA may use a temporary cwd only when its teardown is explicitly marked as
# stopping the managed provider before cleanup.
temp_mark = "longhouse-managed-session-temp-cwd-ok"
provider_command = re.compile(r"longhouse\s+(claude|codex|opencode|cursor)\b")
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
    raise SystemExit(1)

print("managed-session contract check passed")
PY
