"""Dispatch strict provider qualification requests to an exact public profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa import antigravity_hook_qualification
from zerg.qa import antigravity_release_identity
from zerg.qa import claude_real_print_qualification
from zerg.qa import claude_release_identity
from zerg.qa import codex_helm_interrupt
from zerg.qa import codex_release_identity
from zerg.qa import codex_tool_call_result
from zerg.qa import conversation_reset_qualification
from zerg.qa import cursor_release_identity
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
    ("cursor", cursor_release_identity.PROFILE): cursor_release_identity.run,
    ("opencode", opencode_release_identity.PROFILE): opencode_release_identity.run,
    ("opencode", opencode_server_qualification.PROFILE): opencode_server_qualification.run,
    **{
        (provider, profile): (
            lambda request_path, output_root, provider=provider: conversation_reset_qualification.run(provider, request_path, output_root)
        )
        for provider, profile in conversation_reset_qualification.PROFILE_BY_PROVIDER.items()
    },
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
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--profile")
    parser.add_argument("--provider-bin", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    request_path = args.request
    if request_path is None:
        if not args.provider or not args.profile or args.provider_bin is None:
            parser.error("either --request or --provider/--profile/--provider-bin is required")
        expected_profile = conversation_reset_qualification.PROFILE_BY_PROVIDER.get(args.provider)
        if args.profile != expected_profile:
            parser.error("local request construction currently supports conversation-reset profiles")
        binary = args.provider_bin.expanduser().resolve(strict=True)
        version_result = subprocess.run([str(binary), "--version"], text=True, capture_output=True, timeout=15, check=False)
        version_line = version_result.stdout.strip().splitlines()[0] if version_result.stdout.strip() else ""
        match = conversation_reset_qualification._VERSION_LINES[args.provider].search(version_line)  # noqa: SLF001
        if version_result.returncode != 0 or match is None:
            parser.error(f"provider version probe did not match {args.provider} grammar")
        expected_version = match.groupdict().get("version") or match.group(0)
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        git_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        args.output_root.parent.mkdir(parents=True, exist_ok=True)
        request_path = args.output_root.with_name(f"{args.output_root.name}.request.json")
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "provider": args.provider,
                    "profile": args.profile,
                    "provider_bin": str(binary),
                    "expected_provider_version": expected_version,
                    "expected_executable_identity": f"sha256:{digest}",
                    "expected_provider_build_identity": f"sha256:{digest}",
                    "expected_provider_build_granularity": "single_asset",
                    "invocation_id": str(uuid4()),
                    "producer_class": "local_diagnostic",
                    "producer_version": "1",
                    "run_reference": datetime.now(UTC).isoformat(),
                    "longhouse_git_sha": git_result.stdout.strip(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    try:
        result = run(request_path, args.output_root)
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
