#!/usr/bin/env python3
"""Run the existing real-session iOS proof sequentially from a case manifest.

The same name/session_id/markers manifest is accepted by the web proof. iOS cases
also require source_path to prove viewing/reopening does not mutate provider history.
No provider is launched and no phone is used by this runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
from zerg.qa.console_served_state_core import _defaults  # noqa: E402


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_ui(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = ["make", "ios-ui-shot", "TEST=LiveSessionFidelityUITests/testRealSessionColdOpenAndReopen"]
    with subprocess.Popen(
        command, cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=600)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            if isinstance(error, KeyboardInterrupt):
                raise
            return subprocess.CompletedProcess(command, 124, stdout, stderr + "\nUI proof timed out")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    if not isinstance(cases, list) or not cases:
        parser.error("cases must be a nonempty array")
    if any(not isinstance(case, dict) for case in cases):
        parser.error("each case must be an object")
    destination = os.environ.get("IOS_DESTINATION", "")
    if not destination or "platform=iOS Simulator," not in destination:
        parser.error("IOS_DESTINATION must explicitly select an iOS Simulator")
    default_url, default_token = _defaults()
    server_url = os.environ.get("LONGHOUSE_FIDELITY_SERVER_URL") or default_url
    token = os.environ.get("LONGHOUSE_FIDELITY_AUTH_TOKEN") or default_token
    if not server_url or not token:
        parser.error("Runtime Host URL and token are required")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output or ROOT / "artifacts/terminal-fidelity" / f"ios-{stamp}" / "summary.json"
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    results = []

    def save_summary() -> None:
        status = "running" if len(results) < len(cases) else ("pass" if all(item["status"] == "pass" for item in results) else "fail")
        summary = {
            "run_id": stamp,
            "status": status,
            "case_count": len(cases),
            "completed_cases": len(results),
            "server_url": server_url,
            "destination": destination,
            "cases": results,
        }
        output.write_text(json.dumps(summary, indent=2) + "\n")

    # Retire a previous output before launching anything. An interrupted run
    # must never leave yesterday's green verdict under today's output path.
    save_summary()
    for index, case in enumerate(cases):
        result = {"name": case.get("name"), "session_id": case.get("session_id"), "status": "fail"}
        results.append(result)
        try:
            markers = case.get("markers")
            if not case.get("name") or not case.get("session_id") or not isinstance(markers, list) or not markers:
                raise ValueError("case requires name, session_id and nonempty markers")
            if any(not isinstance(marker, str) or not marker for marker in markers):
                raise ValueError("markers must be nonempty strings")
            source = Path(case["source_path"])
            before = digest(source)
            result.update(source_path=str(source), source_sha256_before=before)
            environment = {
                **os.environ,
                "LONGHOUSE_FIDELITY_SERVER_URL": server_url,
                "LONGHOUSE_FIDELITY_AUTH_TOKEN": token,
                "LONGHOUSE_FIDELITY_SESSION_ID": case["session_id"],
                "LONGHOUSE_FIDELITY_MARKERS_JSON": json.dumps(markers),
            }
            print(f"[fidelity] {case['name']} session={case['session_id']}", flush=True)
            completed = run_ui(environment)
            log = (completed.stdout + completed.stderr).replace(token, "[REDACTED]")
            log_path = output.parent / f"{output.stem}-{index}.log"
            log_path.write_text(log)
            result.update(exit_code=completed.returncode, log=str(log_path), source_sha256_after=digest(source))
            if result["source_sha256_after"] != before:
                raise RuntimeError("provider source changed during read-only client verification")
            directories = set(re.findall(r"artifacts/ios-ui-shot/[0-9TZ]+", log))
            if len(directories) != 1:
                raise RuntimeError("runner did not identify one unique screenshot evidence directory")
            directory = ROOT / directories.pop()
            receipts = [json.loads(path.read_text()) for path in sorted(directory.glob("fidelity-*-metrics*.json"))]
            result.update(artifacts=str(directory), receipts=receipts)
            if completed.returncode != 0:
                raise RuntimeError("real iOS client proof failed; inspect retained screenshots and log")
            phases = {receipt.get("phase") for receipt in receipts}
            if (
                phases != {"cold-open", "terminate-reopen"}
                or len(receipts) != 2
                or any(
                    receipt.get("status") != "pass"
                    or receipt.get("paintedFinalMarkerCount") != 1
                    or receipt.get("sessionID") != case["session_id"]
                    or receipt.get("markers") != markers
                    for receipt in receipts
                )
            ):
                raise RuntimeError("both phases must prove actual final-reply pixels for this case")
            result["status"] = "pass"
        except Exception as error:
            result["error"] = str(error).replace(token, "[REDACTED]")
        finally:
            save_summary()
        print(f"[fidelity] {result['name']}: {result['status']}", flush=True)
    print(f"Evidence: {output}")
    return 0 if all(item["status"] == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
