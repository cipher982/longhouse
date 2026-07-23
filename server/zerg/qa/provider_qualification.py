"""Dispatch strict provider qualification requests to an exact public profile."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from zerg.qa import antigravity_hook_qualification
from zerg.qa import antigravity_release_identity
from zerg.qa import claude_real_print_qualification
from zerg.qa import claude_release_identity
from zerg.qa import codex_helm_interrupt
from zerg.qa import codex_release_identity
from zerg.qa import codex_tool_call_result
from zerg.qa import opencode_release_identity
from zerg.qa import opencode_server_qualification

_PROFILES = {
    ("antigravity", antigravity_release_identity.PROFILE): antigravity_release_identity.run,
    ("antigravity", antigravity_hook_qualification.PROFILE): antigravity_hook_qualification.run,
    ("claude", claude_release_identity.PROFILE): claude_release_identity.run,
    ("claude", claude_real_print_qualification.PROFILE): claude_real_print_qualification.run,
    ("codex", codex_release_identity.PROFILE): codex_release_identity.run,
    ("codex", codex_helm_interrupt.PROFILE): codex_helm_interrupt.run,
    ("codex", codex_tool_call_result.PROFILE): codex_tool_call_result.run,
    ("opencode", opencode_release_identity.PROFILE): opencode_release_identity.run,
    ("opencode", opencode_server_qualification.PROFILE): opencode_server_qualification.run,
}


def _profile_key(request_path: Path) -> tuple[str, str]:
    try:
        payload: Any = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise codex_release_identity.RequestError(f"invalid request JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise codex_release_identity.RequestError("request must be an object")
    provider = payload.get("provider")
    profile = payload.get("profile")
    if not isinstance(provider, str) or not isinstance(profile, str):
        raise codex_release_identity.RequestError("provider and profile must be strings")
    return provider, profile


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    key = _profile_key(request_path)
    runner = _PROFILES.get(key)
    if runner is None:
        raise codex_release_identity.RequestError("unsupported provider/profile")
    return runner(request_path, output_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.request, args.output_root)
    except codex_release_identity.RequestError as exc:
        if args.json:
            print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
