#!/usr/bin/env python3
"""Print the newest main commit whose CI run passed, the base a main push's path filters diff from.

A main push used to diff against the push before it. Main CI drops a queued run when a newer push
arrives, so the commits of a dropped run were never path-gated by anyone: on 2026-11-28 an engine fix
landed in a dropped run, the next run saw only a script change, skipped the engine and Terminal Truth
lanes, and the release then waited on a Terminal Truth verdict nothing would ever produce. Diffing
against the last green commit runs every lane the not-yet-proven range touches.

Prints nothing (and the caller falls back to the previous push) when the API is unreachable.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable

Fetch = Callable[[str], dict]


def _get(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def last_green_main(fetch: Fetch, api: str, repo: str, run_id: int) -> str | None:
    this_run = fetch(f"{api}/repos/{repo}/actions/runs/{run_id}")
    workflow_id = this_run["workflow_id"]
    listed = fetch(f"{api}/repos/{repo}/actions/workflows/{workflow_id}/runs?branch=main&event=push&status=success&per_page=5")
    for run in listed.get("workflow_runs", []):
        sha = str(run.get("head_sha") or "")
        if sha and sha != this_run.get("head_sha"):
            return sha
    return None


def main(fetch: Fetch = _get) -> int:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    try:
        sha = last_green_main(fetch, api, os.environ["GITHUB_REPOSITORY"], int(os.environ["GITHUB_RUN_ID"]))
    except Exception as error:  # noqa: BLE001 - falling back to the previous push is always safe to try
        print(f"last green main lookup failed: {error}", file=sys.stderr)
        sha = None
    if sha:
        print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
