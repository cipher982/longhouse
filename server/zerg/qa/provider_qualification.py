"""Dispatch strict provider qualification requests to an exact public profile."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from zerg.qa import codex_release_identity
from zerg.qa import codex_tool_call_result

_PROFILES = {
    ("codex", codex_release_identity.PROFILE): codex_release_identity.run,
    ("codex", codex_tool_call_result.PROFILE): codex_tool_call_result.run,
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
