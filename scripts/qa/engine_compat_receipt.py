#!/usr/bin/env python3
"""Write the per-commit receipt of the previous-release engine smoke.

`make engine-compat` ships a transcript with the newest released engine against
the candidate server built from this commit (release-rings.md change 5). CI
uploads this receipt as the artifact `engine-compat-<sha>`, and
`make promote-production` refuses a commit that has no receipt saying `passed`
(scripts/ops/promotion_gates.py): the server keeps accepting the engine users
already run before the new-tenant default moves.

The result is derived from the test report, never from an exit code alone. A run
whose tests were all skipped exits 0 and proves nothing, so `passed` needs at
least one executed test and no failure, error or skip. A commit with no previous
release for this platform is recorded as `skipped`, which no promotion accepts.

Exit 0 when a receipt was written with result passed or skipped, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

SCHEMA = "longhouse.engine-compat.v1"
# What the smoke runs; recorded so a reader knows what "passed" covers.
TEST = "server/tests/integration/test_shipper_e2e.py::TestClaudeShipping"


class NotPassed(Exception):
    pass


def junit_counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return {
        key: sum(int(suite.get(key, 0)) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def build_receipt(
    *, metadata: dict[str, Any], junit: Path | None, source_sha: str, now: datetime, environ: dict[str, str]
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "source_sha": source_sha,
        "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "subject": "server built from source_sha (not the runtime image; the image is bound to it by its source label)",
        "test": TEST,
        "run": {
            "repository": environ.get("GITHUB_REPOSITORY"),
            "workflow": environ.get("GITHUB_WORKFLOW"),
            "id": environ.get("GITHUB_RUN_ID"),
            "attempt": environ.get("GITHUB_RUN_ATTEMPT"),
        },
    }
    if "skipped" in metadata:
        return {**receipt, "result": "skipped", "reason": metadata["skipped"]}
    for key in ("previous_release_tag", "asset", "platform", "engine_sha256"):
        if not metadata.get(key):
            raise NotPassed(f"the downloaded engine's metadata has no {key}")
    if junit is None or not junit.exists():
        raise NotPassed("no test report: the smoke did not run")
    counts = junit_counts(junit)
    if counts["tests"] < 1 or counts["failures"] or counts["errors"] or counts["skipped"]:
        raise NotPassed(f"the smoke did not pass cleanly: {counts}")
    return {
        **receipt,
        "result": "passed",
        "previous_release_tag": metadata["previous_release_tag"],
        "previous_engine": {key: metadata[key] for key in ("asset", "platform", "engine_sha256")},
        "counts": counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata", type=Path, required=True, help="Written by download_previous_engine.py --metadata.")
    parser.add_argument("--junit", type=Path, help="pytest --junitxml report of the smoke (not needed when it was skipped).")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", help="Defaults to the checkout's HEAD.")
    args = parser.parse_args(argv)
    source_sha = args.source_sha or subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        receipt = build_receipt(
            metadata=json.loads(args.metadata.read_text(encoding="utf-8")),
            junit=args.junit,
            source_sha=source_sha,
            now=datetime.now(timezone.utc),
            environ=dict(os.environ),
        )
    except NotPassed as exc:
        print(f"engine-compat: {exc}", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"engine-compat: {receipt['result']} for {source_sha[:9]} -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
