"""Bounded diagnostic proof for the exact Codex release binary."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from zerg.qa import provider_release_identity as identity

SCHEMA_VERSION = identity.SCHEMA_VERSION
PROFILE = "codex_release_identity_v1"
SCENARIO_ID = "codex_release_identity"
SCENARIO_REVISION = identity.SCENARIO_REVISION
ASSERTIONS = identity.ASSERTIONS
TIMEOUT_SECONDS = identity.TIMEOUT_SECONDS
_SEMVER = identity.SEMVER
_VERSION_LINE = re.compile(rf"^codex-cli (?P<version>{_SEMVER})$")
_IDENTITY = identity.IDENTITY
_REDACTIONS = identity.REDACTIONS
_REQUEST_KEYS = identity.REQUEST_KEYS
RequestError = identity.RequestError

_PROFILE = identity.IdentityProfile(
    provider="codex",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=_VERSION_LINE,
    oracle_source=Path(__file__),
)


def _redact_text(value: str) -> str:
    return identity.redact_text(value)


def _now() -> str:
    return identity.now()


def _sha256(data: bytes) -> str:
    return identity.sha256(data)


def _sha256_file(path: Path) -> str:
    return identity.sha256_file(path)


def _atomic_json(path: Path, payload: Any) -> None:
    identity.atomic_json(path, payload)


def _load_request_for_profile(path: Path, profile: str) -> dict[str, Any]:
    return identity.load_request(path, provider="codex", profile=profile)


def _load_request(path: Path) -> dict[str, Any]:
    return _load_request_for_profile(path, PROFILE)


def _git_sha(root: Path) -> str | None:
    return identity.git_sha(root)


def _git_dirty(root: Path) -> bool:
    return identity.git_dirty(root)


def _preflight(request: dict[str, Any], output_root: Path, repo_root: Path) -> tuple[Path, str, str]:
    return identity.preflight(
        request,
        output_root,
        repo_root,
        git_sha_fn=_git_sha,
        git_dirty_fn=_git_dirty,
    )


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    return identity.run_identity_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        repo_root=Path(__file__).resolve().parents[3],
        timeout_seconds=TIMEOUT_SECONDS,
        git_sha_fn=_git_sha,
        git_dirty_fn=_git_dirty,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.request, args.output_root)
    except RequestError as exc:
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
