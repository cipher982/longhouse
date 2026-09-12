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
    "test-install",
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
    "LONGHOUSE_NATIVE_SMOKE_REMOTE",
    "LONGHOUSE_NATIVE_SMOKE_EXPECTED_VERSION",
    "LONGHOUSE_NATIVE_SMOKE_EXPECTED_COMMIT",
    "LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG",
}


def capture(*argv: str, timeout: float = 30) -> str:
    return subprocess.check_output(
        argv, cwd=ROOT, text=True, start_new_session=True, timeout=timeout
    ).strip()


def owned_run_ids(repository: str, title: str) -> list[str]:
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
    return [str(row["databaseId"]) for row in runs if row.get("displayTitle") == title]


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
    if args.target == "test-install":
        if options.get("MODE") not in (None, ""):
            raise ValueError("test-install does not support MODE; use installer options")
        if options.get("LONGHOUSE_NATIVE_SMOKE_REMOTE") not in (
            None,
            "",
            "0",
            "1",
        ):
            raise ValueError("LONGHOUSE_NATIVE_SMOKE_REMOTE must be 0 or 1")
    elif options.get("MODE") not in (
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
        "dispatch_accepted": False,
        "cleanup": False,
    }
    receipt_path = artifacts / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    old_handlers = {}
    watch = None
    interrupted = 0
    dispatch_started = False

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = signum
        # Once dispatch has started, honor the interrupt and reconcile by the
        # unique request ID rather than leaving an accepted run undiscovered.
        if dispatch_started:
            raise KeyboardInterrupt

    def reconcile_and_cancel() -> None:
        deadline = time.monotonic() + 120
        last_error = None
        while time.monotonic() < deadline:
            try:
                run_ids = owned_run_ids(repository, title)
            except Exception as exc:
                run_ids = []
                last_error = f"request reconciliation failed: {exc}"
            if run_ids:
                receipt["reconciled_run_ids"] = run_ids
                receipt["run_id"] = run_ids[0]
                receipt["url"] = (
                    f"https://github.com/{repository}/actions/runs/{run_ids[0]}"
                )
                cancel_errors = []
                for run_id in run_ids:
                    try:
                        cancel = subprocess.run(
                            ["gh", "run", "cancel", run_id, "--repo", repository],
                            check=False,
                            timeout=max(1, min(30, deadline - time.monotonic())),
                        )
                        if cancel.returncode:
                            cancel_errors.append(
                                f"cancel {run_id} exited {cancel.returncode}"
                            )
                    except Exception as exc:
                        cancel_errors.append(f"cancel {run_id} failed: {exc}")
                try:
                    results = [
                        json.loads(
                            capture(
                                "gh",
                                "run",
                                "view",
                                run_id,
                                "--repo",
                                repository,
                                "--json",
                                "status,conclusion",
                                timeout=max(1, min(30, deadline - time.monotonic())),
                            )
                        )
                        for run_id in run_ids
                    ]
                    receipt.update(results[0])
                    if all(result["status"] == "completed" for result in results):
                        receipt["cleanup"] = True
                        return
                    if cancel_errors:
                        last_error = "; ".join(cancel_errors)
                except Exception as exc:
                    last_error = f"cancellation confirmation failed: {exc}"
            if time.monotonic() >= deadline:
                break
            time.sleep(min(2, deadline - time.monotonic()))
        receipt["cleanup"] = False
        detail = f"Cancellation unresolved for owned request {request_id}"
        if last_error:
            detail += f": {last_error}"
        receipt["error"] = (
            f"{receipt['error']}; {detail}" if receipt.get("error") else detail
        )
        print(receipt["error"], file=sys.stderr)

    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, interrupt)
    try:
        dispatch_started = True
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
            timeout=min(args.timeout, 60),
        )
        receipt["dispatch_accepted"] = True
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            matches = owned_run_ids(repository, title)
            if matches:
                receipt["run_id"] = matches[0]
                break
            time.sleep(2)
        if receipt["run_id"] is None:
            raise RuntimeError(
                f"dispatch accepted but run not yet discoverable; request {request_id}"
            )
        run_id = str(receipt["run_id"])
        receipt["url"] = f"https://github.com/{repository}/actions/runs/{run_id}"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
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
    except KeyboardInterrupt as exc:
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        receipt["exit_code"] = 128 + (interrupted or signal.SIGINT)
        receipt["error"] = str(exc) or "native isolation interrupted"
        if dispatch_started:
            reconcile_and_cancel()
        return receipt["exit_code"]
    except Exception as exc:
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        receipt["exit_code"] = 124 if isinstance(exc, subprocess.TimeoutExpired) else 2
        receipt["error"] = str(exc)
        if dispatch_started:
            reconcile_and_cancel()
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
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"[native-test-isolation] receipt: {receipt_path}", flush=True)


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
