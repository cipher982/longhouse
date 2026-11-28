#!/usr/bin/env python3
"""Cancel a failed main CI run when a newer main commit is waiting behind it.

Main CI shares one concurrency group: a started run always finishes, and one
newer push waits behind it. A run with a failed job can no longer release, so
every minute its remaining jobs run (Terminal Truth alone is ~15 minutes) holds
up the newer commit, which contains this one and will be tested in full. With
nothing waiting, the failed run finishes as before, so its full verdict is kept.

A cancelled run reads as superseded to the release planner and the ship
watcher, which then follow the newer commit; a failure that commit still
carries is reported on its own run.

Runs as the last step of each CI job under `if: failure()` on main pushes. It
never fails the step: the job has already failed and this is only a shortcut.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable

WAITING_STATUSES = ("pending", "queued", "waiting", "requested")

Fetch = Callable[[str, str], dict]


def _http(method: str, url: str) -> dict:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
    return json.loads(body) if body else {}


def newer_waiting_run(fetch: Fetch, api: str, repo: str, run_id: int) -> int | None:
    """The id of a newer main push run of this workflow that is waiting, if any."""

    this_run = fetch("GET", f"{api}/repos/{repo}/actions/runs/{run_id}")
    workflow_id = this_run["workflow_id"]
    number = int(this_run["run_number"])
    for status in WAITING_STATUSES:
        listed = fetch(
            "GET",
            f"{api}/repos/{repo}/actions/workflows/{workflow_id}/runs?branch=main&event=push&status={status}&per_page=20",
        )
        for run in listed.get("workflow_runs", []):
            if int(run["run_number"]) > number and int(run["id"]) != run_id:
                return int(run["id"])
    return None


def main(fetch: Fetch = _http) -> int:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    try:
        newer = newer_waiting_run(fetch, api, repo, run_id)
        if newer is None:
            print("No newer main commit is waiting; this failed run finishes in full.")
            return 0
        print(f"Run {newer} for a newer main commit is waiting; cancelling this failed run so it starts now.")
        fetch("POST", f"{api}/repos/{repo}/actions/runs/{run_id}/cancel")
    except Exception as error:  # noqa: BLE001 - a shortcut must never turn into a new failure
        print(f"Could not yield this failed run: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
