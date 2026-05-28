#!/usr/bin/env python3
"""Tests for stable local provider live-proof publishing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = REPO_ROOT / "scripts/qa/provider-live-proof-publish.py"


def _write_fake_claude(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        r"""#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.153-fake (Claude Code)")
    raise SystemExit(0)

if args == ["--help"]:
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

if args == ["--dangerously-load-development-channels", "server:longhouse-channel", "--help"]:
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

print("unexpected fake claude args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _publisher_env(root: Path, *, include_fake_claude: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LONGHOUSE_PROVIDER_LIVE_PROOF_DIR", None)
    env["LONGHOUSE_PROVIDER_RELEASE_STATUS_CONFIG"] = str(root / "missing-provider-status.env")
    if include_fake_claude:
        fake_claude = _write_fake_claude(root / "bin" / "claude")
        env["PATH"] = f"{fake_claude.parent}{os.pathsep}{env.get('PATH', '')}"
    else:
        empty_bin = root / "empty-bin"
        empty_bin.mkdir()
        env["PATH"] = str(empty_bin)
    return env


def _run_publisher(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PUBLISHER), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_publishes_stable_sidecar_from_packaged_live_canary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proof_dir = root / "proof"
        result = _run_publisher(
            [
                "--repo-root",
                str(root / "repo"),
                "--provider",
                "claude",
                "--evidence-root",
                str(root / "evidence"),
                "--proof-dir",
                str(proof_dir),
                "--json",
            ],
            env=_publisher_env(root, include_fake_claude=True),
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["results"][0]["status"] == "published"
        stable = proof_dir / "claude.json"
        artifact = json.loads(stable.read_text(encoding="utf-8"))
        assert artifact["artifact_kind"] == "provider_live_canary"
        assert artifact["provider"] == "claude"
        assert artifact["operation_evidence"]["launch_local"]["level"] == "live_no_token"


def test_publishes_red_sidecar_when_provider_binary_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proof_dir = root / "proof"
        result = _run_publisher(
            [
                "--repo-root",
                str(root / "repo"),
                "--provider",
                "opencode",
                "--evidence-root",
                str(root / "evidence"),
                "--proof-dir",
                str(proof_dir),
                "--json",
            ],
            env=_publisher_env(root, include_fake_claude=False),
        )

        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["results"][0]["status"] == "published"
        artifact = json.loads((proof_dir / "opencode.json").read_text(encoding="utf-8"))
        assert artifact["artifact_kind"] == "provider_live_canary"
        assert artifact["verdict"] == "red"
        assert artifact["failure_code"] == "provider_binary_not_found"


def test_defaults_proof_dir_to_longhouse_home() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        longhouse_home = root / ".longhouse-dev"
        env = _publisher_env(root, include_fake_claude=True)
        env["LONGHOUSE_HOME"] = str(longhouse_home)

        result = _run_publisher(
            [
                "--repo-root",
                str(root / "repo"),
                "--provider",
                "claude",
                "--evidence-root",
                str(root / "evidence"),
                "--json",
            ],
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["proof_dir"] == str((longhouse_home / "provider-live-proof").resolve())
        assert (longhouse_home / "provider-live-proof" / "claude.json").exists()


if __name__ == "__main__":
    test_publishes_stable_sidecar_from_packaged_live_canary()
    test_publishes_red_sidecar_when_provider_binary_is_missing()
    test_defaults_proof_dir_to_longhouse_home()
