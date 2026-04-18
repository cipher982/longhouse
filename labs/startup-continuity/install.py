#!/usr/bin/env python3
"""Opt-in installer for the startup-continuity lab.

Rewrites the already-installed Claude and Codex hook scripts in place to add a
SessionStart fetch of ``/api/agents/sessions/startup-context`` and inject the
rendered block as ``hookSpecificOutput.additionalContext``.

This is not part of the default Longhouse install. It modifies agent
behavior at session start; it is not observational.

Usage:
    python labs/startup-continuity/install.py           # enable (rewrites hooks)
    python labs/startup-continuity/install.py --check   # show status, no writes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CLAUDE_HOOK = Path.home() / ".claude" / "hooks" / "longhouse-hook.sh"
CODEX_HOOK = Path.home() / ".codex" / "hooks" / "longhouse-codex-hook.sh"

MARKER = "# LAB:startup-continuity"

CLAUDE_INJECTION = """
# LAB:startup-continuity -- SessionStart fetch/inject (opt-in)
if [[ "$EVENT" == "SessionStart" ]] && [[ -n "$CWD" ]]; then
  REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null | tr -d '\\r')
  if [[ -n "$REPO_ROOT" ]]; then
    LAB_PROJECT=$(basename "$REPO_ROOT")
  else
    LAB_PROJECT=$(basename "$CWD")
  fi
  if [[ -n "$LAB_PROJECT" ]]; then
    LAB_TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"
    LAB_URL="${LONGHOUSE_HOOK_URL:-}"
    if [[ -z "$LAB_TOKEN" ]] || [[ -z "$LAB_URL" ]]; then
      LAB_TOKEN_FILE="$LONGHOUSE_HOME/machine/device-token"
      LAB_STATE_FILE="$LONGHOUSE_HOME/machine/state.json"
      if [[ -f "$LAB_TOKEN_FILE" ]] && [[ -f "$LAB_STATE_FILE" ]]; then
        LAB_TOKEN=$(tr -d '[:space:]' < "$LAB_TOKEN_FILE")
        LAB_URL=$(jq -r '.runtime_url // empty' "$LAB_STATE_FILE" 2>/dev/null | tr -d '[:space:]')
      fi
    fi
    if [[ -n "$LAB_TOKEN" ]] && [[ -n "$LAB_URL" ]]; then
      LAB_PROJECT_ENC=$(
        python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$LAB_PROJECT" 2>/dev/null \\
          || printf '%s' "$LAB_PROJECT"
      )
      LAB_RESPONSE=$(curl -sf --max-time 5 \\
        -H "X-Agents-Token: $LAB_TOKEN" \\
        "${LAB_URL}/api/agents/sessions/startup-context?project=${LAB_PROJECT_ENC}&limit=5" 2>/dev/null || true)
      if [[ -n "$LAB_RESPONSE" ]]; then
        LAB_CONTEXT=$(printf '%s' "$LAB_RESPONSE" | jq -r '.startup_context // empty' 2>/dev/null)
        if [[ -n "$LAB_CONTEXT" ]]; then
          jq -nc --arg msg "$LAB_CONTEXT" '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": $msg}}'
        fi
      fi
    fi
  fi
fi
# LAB:startup-continuity -- end
"""

# Codex uses the same injection body — it runs against the same
# additionalContext contract.
CODEX_INJECTION = CLAUDE_INJECTION


def _is_enabled(hook_path: Path) -> bool:
    if not hook_path.exists():
        return False
    return MARKER in hook_path.read_text(encoding="utf-8")


def _inject(hook_path: Path, snippet: str) -> bool:
    if not hook_path.exists():
        return False
    text = hook_path.read_text(encoding="utf-8")
    if MARKER in text:
        return False
    if "exit 0\n" not in text:
        raise RuntimeError(
            f"{hook_path} does not end with the expected `exit 0` marker; "
            "refusing to splice lab snippet into an unknown layout."
        )
    rewritten = text.replace("exit 0\n", snippet.lstrip("\n") + "\nexit 0\n", 1)
    hook_path.write_text(rewritten, encoding="utf-8")
    return True


def enable() -> int:
    any_written = False
    for hook_path, snippet, label in (
        (CLAUDE_HOOK, CLAUDE_INJECTION, "claude"),
        (CODEX_HOOK, CODEX_INJECTION, "codex"),
    ):
        if not hook_path.exists():
            print(f"[{label}] hook not installed, skipping: {hook_path}")
            continue
        if _is_enabled(hook_path):
            print(f"[{label}] already enabled: {hook_path}")
            continue
        _inject(hook_path, snippet)
        any_written = True
        print(f"[{label}] enabled startup-continuity in {hook_path}")

    if not any_written:
        print("No hooks changed. Re-run `longhouse connect --install` first if needed.")
    return 0


def check() -> int:
    for hook_path, label in ((CLAUDE_HOOK, "claude"), (CODEX_HOOK, "codex")):
        if not hook_path.exists():
            print(f"[{label}] hook not installed: {hook_path}")
            continue
        status = "enabled" if _is_enabled(hook_path) else "disabled"
        print(f"[{label}] {status}: {hook_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report status without writing.")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return enable()


if __name__ == "__main__":
    sys.exit(main())
