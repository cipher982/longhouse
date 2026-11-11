#!/usr/bin/env python3
"""Post the `longhouse/terminal-truth` commit status for the commit this run is on.

  terminal_truth_status.py STATE DESCRIPTION

STATE is pending, success, failure or error. Reads GH_TOKEN, GITHUB_REPOSITORY, GITHUB_SHA, GITHUB_SERVER_URL and
GITHUB_RUN_ID from the Actions environment. Uses the REST API directly because the Linux runner has no `gh`.
The context name is the one the dogfood promotion requirement reads; do not rename it. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

CONTEXT = "longhouse/terminal-truth"
STATES = {"pending", "success", "failure", "error"}


def payload(state: str, description: str, env: dict[str, str]) -> dict[str, str]:
    if state not in STATES:
        raise SystemExit(f"state must be one of {sorted(STATES)}, not {state!r}")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    return {
        "state": state,
        "context": CONTEXT,
        "description": description[:140],
        "target_url": f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    env = dict(os.environ)
    body = json.dumps(payload(argv[1], argv[2], env)).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{env['GITHUB_REPOSITORY']}/statuses/{env['GITHUB_SHA']}",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {env['GH_TOKEN']}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https API host
        print(f"posted {argv[1]} for {env['GITHUB_SHA'][:12]}: HTTP {response.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
