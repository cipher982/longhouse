#!/usr/bin/env python3
"""Dispatch native tests to a fresh, standard GitHub-hosted macOS VM.

Requires a clean, pushed revision in a public repository. No local Xcode,
provider, simulator, Keychain, paid larger runner, or VM networking fallback.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = "native-test-isolation.yml"
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


def capture(*argv: str) -> str:
    return subprocess.check_output(
        argv, cwd=ROOT, text=True, start_new_session=True, timeout=30
    ).strip()


def run(args: argparse.Namespace) -> int:
    if args.target not in TARGETS:
        raise ValueError(
            "target needs an explicit external-data/device proof, not the native fixture lane"
        )
    options = json.loads(args.options_json.read_text())
    if not isinstance(options, dict) or any(
        key not in OPTIONS or not isinstance(value, str)
        for key, value in options.items()
    ):
        raise ValueError("invalid native test options")
    if options.get("MODE") not in (
        None,
        "",
        "test",
        "smoke",
        "render-fixtures",
        "render-trust-states",
        "xcuitest",
    ):
        raise ValueError("native fixture mode cannot use a live Runtime Host")
    if capture("git", "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError(
            "native CI needs a clean source revision; commit and push the worktree first"
        )
    sha = capture("git", "rev-parse", "HEAD")
    repo = json.loads(
        capture("gh", "repo", "view", "--json", "nameWithOwner,visibility")
    )
    if repo["visibility"] != "PUBLIC":
        raise ValueError(
            "native dispatch requires a public repository to avoid billed macOS minutes"
        )
    repository = repo["nameWithOwner"]
    capture("gh", "api", f"repos/{repository}/commits/{sha}", "--jq", ".sha")
    request_id = uuid.uuid4().hex
    title = f"Native isolation {request_id}"
    artifacts = args.artifact_dir.resolve()
    artifacts.mkdir(parents=True, mode=0o700, exist_ok=False)
    receipt = {
        "source_sha": sha,
        "repository": repository,
        "request_id": request_id,
        "target": args.target,
        "lane": "github-standard-macos",
        "run_id": None,
        "cleanup": False,
    }
    (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    old_handlers = {}
    watch = None
    interrupted = 0

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = signum
        # Finish identifying an accepted dispatch before honoring cancellation.
        if receipt["run_id"] is not None:
            raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, interrupt)
    try:
        subprocess.run(
            [
                "gh",
                "workflow",
                "run",
                WORKFLOW,
                "--repo",
                repository,
                "--ref",
                "main",
                "-f",
                f"source_sha={sha}",
                "-f",
                f"request_id={request_id}",
                "-f",
                f"target={args.target}",
                "-f",
                "options_json=" + json.dumps(options),
            ],
            check=True,
            cwd=ROOT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            runs = json.loads(
                capture(
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    repository,
                    "--workflow",
                    WORKFLOW,
                    "--event",
                    "workflow_dispatch",
                    "--limit",
                    "100",
                    "--json",
                    "databaseId,displayTitle",
                )
            )
            matches = [row for row in runs if row["displayTitle"] == title]
            if matches:
                receipt["run_id"] = matches[0]["databaseId"]
                break
            time.sleep(2)
        if receipt["run_id"] is None:
            raise RuntimeError(
                f"dispatch accepted but run not yet discoverable; request {request_id}"
            )
        run_id = str(receipt["run_id"])
        receipt["url"] = f"https://github.com/{repository}/actions/runs/{run_id}"
        (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"[native-test-isolation] {receipt['url']} source={sha}", flush=True)
        if interrupted:
            raise KeyboardInterrupt
        watch = subprocess.Popen(
            ["gh", "run", "watch", run_id, "--repo", repository, "--exit-status"],
            start_new_session=True,
        )
        status = watch.wait(timeout=args.timeout)
        result = json.loads(
            capture(
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repository,
                "--json",
                "status,conclusion",
            )
        )
        receipt.update(result)
        receipt["cleanup"] = result["status"] == "completed"
        download = subprocess.run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                repository,
                "--name",
                f"native-isolation-{request_id}",
                "--dir",
                str(artifacts / "evidence"),
            ],
            check=False,
            timeout=120,
        )
        receipt["evidence_downloaded"] = download.returncode == 0
        receipt["exit_code"] = status or download.returncode
        return receipt["exit_code"]
    except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        receipt["exit_code"] = (
            124
            if isinstance(exc, subprocess.TimeoutExpired)
            else 128 + (interrupted or signal.SIGINT)
        )
        if receipt["run_id"]:
            run_id = str(receipt["run_id"])
            subprocess.run(
                ["gh", "run", "cancel", run_id, "--repo", repository],
                check=False,
                timeout=30,
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                result = json.loads(
                    capture(
                        "gh",
                        "run",
                        "view",
                        run_id,
                        "--repo",
                        repository,
                        "--json",
                        "status,conclusion",
                    )
                )
                receipt.update(result)
                receipt["cleanup"] = result["status"] == "completed"
                if receipt["cleanup"]:
                    break
                time.sleep(2)
            if not receipt["cleanup"]:
                receipt["error"] = (
                    f"Cancellation unresolved; inspect {receipt.get('url', run_id)}"
                )
                print(receipt["error"], file=sys.stderr)
        return receipt["exit_code"]
    finally:
        if watch is not None and watch.poll() is None:
            watch.terminate()
            try:
                watch.wait(timeout=10)
            except subprocess.TimeoutExpired:
                watch.kill()
                watch.wait()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(
            f"[native-test-isolation] receipt: {artifacts / 'receipt.json'}", flush=True
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--options-json", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    try:
        return run(args)
    except (
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"native-test-isolation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
