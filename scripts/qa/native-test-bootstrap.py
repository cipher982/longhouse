#!/usr/bin/env python3
"""Run native fixture tests only inside an ephemeral GitHub-hosted macOS VM."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = Path("/tmp/longhouse-test-isolated")
TARGETS = {
    "test-ios",
    "test-ios-perf",
    "test-ios-session-open",
    "test-mobile-chat",
    "test-mobile-chat-stress",
    "test-runtime-packaging-macos",
    "menubar-harness",
    "simlab-run",
    "test-e2e-onboarding",
    "ios-ui-shot",
    "ios-previews",
    "benchmark-ios-transcript",
}
OPTIONS = {
    "TEST",
    "MODE",
    "SCENARIOS",
    "CARGO_PROFILE",
    "VERBOSE",
    "IOS_TEST_SCHEMES",
    "PROJECT",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    args = parser.parse_args()
    if (
        sys.platform != "darwin"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or MARKER.exists()
    ):
        parser.error(
            "native tests require a fresh GitHub-hosted macOS VM; never the developer login"
        )
    options = json.loads(os.environ.get("NATIVE_OPTIONS_JSON", "{}"))
    if not isinstance(options, dict) or any(
        key not in OPTIONS or not isinstance(value, str)
        for key, value in options.items()
    ):
        parser.error("invalid native fixture options")
    if options.get("MODE") not in (
        None,
        "",
        "test",
        "smoke",
        "render-fixtures",
        "render-trust-states",
        "xcuitest",
    ):
        parser.error("live native modes require a separately authorized proof")
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    output = runner_temp / "native-isolation-evidence" / args.target
    output.mkdir(mode=0o700, parents=True)
    scratch = Path(tempfile.mkdtemp(prefix="longhouse-native-", dir=runner_temp))
    home = scratch / "home"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TMPDIR": str(scratch / "tmp"),
        "USER": "longhouse-test",
        "LOGNAME": "longhouse-test",
        "LANG": "en_US.UTF-8",
        "CI": "1",
        "TESTING": "1",
        "LONGHOUSE_TEST_ISOLATED": "1",
        "LONGHOUSE_TEST_ROOT": str(scratch),
        "LONGHOUSE_HOME": str(home / ".longhouse"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CURSOR_CONFIG_DIR": str(home / ".cursor"),
        "PI_CODING_AGENT_DIR": str(home / ".pi/agent"),
        "LONGHOUSE_OMP_CONFIG_DIR": str(home / ".omp"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "E2E_DB_DIR": str(scratch / "e2e-db"),
        "E2E_ARTIFACT_DIR": str(scratch / "artifacts/e2e"),
        "IOS_DERIVED_DATA_PATH": str(scratch / "derived-data"),
        "IOS_RESULTS_DIR": str(scratch / "artifacts/ios"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "AWS_EC2_METADATA_DISABLED": "true",
        "LLM_DISABLED": "1",
        "AUTH_DISABLED": "1",
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        "CARGO_HOME": str(scratch / "cargo"),
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
        "BUN_INSTALL_CACHE_DIR": str(scratch / "bun-cache"),
        **options,
    }
    for key in ("HOME", "TMPDIR", "LONGHOUSE_HOME", "CARGO_HOME"):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    child = None
    handlers = {}
    receipt = {
        "target": args.target,
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "lane": "github-standard-macos",
        "network": "loopback-only",
        "cleanup": False,
    }

    def interrupt(signum, _frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, interrupt)
        # Resolve dependencies before disabling test egress. No provider secrets or
        # login state enter either phase; this entire VM is discarded after the job.
        if args.target in {
            "test-mobile-chat",
            "test-mobile-chat-stress",
            "simlab-run",
            "test-e2e-onboarding",
        }:
            subprocess.run(
                ["bun", "install", "--frozen-lockfile"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        if args.target == "simlab-run":
            subprocess.run(
                ["cargo", "fetch", "--manifest-path", "engine/Cargo.toml", "--locked"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        if args.target in {"simlab-run", "test-e2e-onboarding"}:
            subprocess.run(
                ["uv", "sync", "--project", "server", "--frozen", "--extra", "dev"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        if args.target == "test-e2e-onboarding":
            subprocess.run(
                ["bunx", "playwright", "install", "chromium", "firefox", "webkit"],
                cwd=ROOT / "e2e",
                env=environment,
                check=True,
            )
        if args.target in {"menubar-harness", "test-runtime-packaging-macos"}:
            subprocess.run(
                ["swift", "package", "resolve"],
                cwd=ROOT / "desktop/LonghouseMenuBarHarness",
                env=environment,
                check=True,
            )
        if (
            args.target.startswith("test-ios")
            or args.target.startswith("test-mobile-chat")
            or args.target
            in {"ios-ui-shot", "ios-previews", "benchmark-ios-transcript", "simlab-run"}
        ):
            subprocess.run(
                ["python3", "scripts/build/generate_build_identity.py"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            subprocess.run(
                ["bash", "scripts/build/stage_ios_build_identity.sh"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            subprocess.run(
                [
                    "xcodegen",
                    "--spec",
                    "ios/XcodeHarness/project.yml",
                    "--project-root",
                    "ios/XcodeHarness",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            subprocess.run(
                [
                    "xcodebuild",
                    "-resolvePackageDependencies",
                    "-project",
                    "ios/XcodeHarness/LonghouseIOS.xcodeproj",
                    "-scheme",
                    "Longhouse",
                    "-derivedDataPath",
                    environment["IOS_DERIVED_DATA_PATH"],
                ],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        environment.update({"CARGO_NET_OFFLINE": "true", "UV_OFFLINE": "1"})
        # Keep native frameworks/IPC working, but deny access to hosted services.
        # The security boundary for files, Keychain and GUI is the disposable VM.
        profile = '(version 1)(allow default)(deny network-outbound (remote ip "*:*"))(allow network-outbound (remote ip "localhost:*"))'
        MARKER.touch(exist_ok=False)
        child = subprocess.Popen(
            ["sandbox-exec", "-p", profile, "make", args.target],
            cwd=ROOT,
            env=environment,
            start_new_session=True,
        )
        status = child.wait()
        receipt["exit_code"] = status
        return status
    except KeyboardInterrupt:
        receipt["exit_code"] = 130
        return 130
    finally:
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        # No host files or GUI resources are shared with this VM. The hosted runner
        # lifetime is the final descendant/simulator cleanup boundary, even on SIGKILL.
        try:
            for name, source in (
                ("native", scratch / "artifacts"),
                ("project", ROOT / "artifacts"),
            ):
                if source.exists():
                    shutil.copytree(
                        source,
                        output / name,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(
                            "swift-build", "xcode-project", "*.app"
                        ),
                    )
        finally:
            MARKER.unlink(missing_ok=True)
            shutil.rmtree(scratch)
            receipt["cleanup"] = not scratch.exists() and not MARKER.exists()
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
