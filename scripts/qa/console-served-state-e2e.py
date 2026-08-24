#!/usr/bin/env python3
"""Run the Console served-state proof by hand.

The predicates, the client, the stream watcher and the fault-injection arming all
live in `server/zerg/qa/console_served_state_core.py`, which the factory producer
`zerg.qa.console_served_state` also imports. Keeping one implementation is the
point: a copy here that drifted from what the factory asserts would reproduce the
exact defect class this whole effort exists to close -- two descriptions of the
same truth, quietly disagreeing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from zerg.qa.console_served_state_core import ApiError  # noqa: E402
from zerg.qa.console_served_state_core import console_providers  # noqa: E402
from zerg.qa.console_served_state_core import run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        default="codex",
        help="a provider name, or 'all' to derive the set from schemas/managed_providers.yml",
    )
    parser.add_argument("--device-id", default=os.environ.get("LONGHOUSE_DEVICE_ID") or "cinder")
    parser.add_argument("--cwd", default=str(Path.home() / "git" / "zerg"))
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--turn-timeout", type=float, default=180.0)
    parser.add_argument("--settle-budget", type=float, default=30.0)
    parser.add_argument(
        "--content-budget",
        type=float,
        default=90.0,
        help="separate budget for transcript convergence, which lags state settlement",
    )
    parser.add_argument(
        "--watch-session",
        default=None,
        help="negative control: stream a different session so live delivery must fail",
    )
    parser.add_argument(
        "--drop-terminal",
        action="store_true",
        help="acceptance mode: drop this session's terminal in transit; red is the pass",
    )
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    providers = console_providers() if args.provider == "all" else [args.provider]

    reports: list[dict] = []
    for provider in providers:
        attempt = argparse.Namespace(**{**vars(args), "provider": provider})
        try:
            reports.append(run(attempt))
        except ApiError as error:
            # `adapter_unavailable` is the machine saying it does not offer this
            # provider's Console turn. That is provider availability, not an API
            # failure, and calling it an error blames the wrong system.
            unavailable = error.status == 409 and "adapter_unavailable" in str(error)
            reports.append(
                {
                    "artifact_kind": "console_served_state_e2e",
                    "schema_version": 1,
                    "provider": provider,
                    # The API failing is not a verdict about this provider. Say
                    # so separately, and still count it: a check that could not
                    # run is not a check that passed.
                    "verdict": "unavailable" if unavailable else "error",
                    "failures": [str(error)],
                }
            )
        except RuntimeError as error:
            # A provider that will not start on this machine is a provider
            # install, not a Longhouse contract failure. Recording it as red
            # would leave this permanently red for an unauthenticated CLI and
            # teach everyone to ignore it.
            reports.append(
                {
                    "artifact_kind": "console_served_state_e2e",
                    "schema_version": 1,
                    "provider": provider,
                    "verdict": "unavailable",
                    "failures": [str(error)],
                }
            )

    if len(reports) == 1:
        payload = reports[0]
    else:
        verified = [report["provider"] for report in reports if report["verdict"] == "green"]
        failed = [report["provider"] for report in reports if report["verdict"] in {"red", "error"}]
        if failed:
            verdict = "red"
        elif not verified:
            # Nothing was actually checked. Reporting green here would let a
            # machine that offers no providers, or an instance that 503s on
            # every request, pass as proof -- unknown must stay unknown.
            verdict = "unqualified"
        else:
            verdict = "green"
        payload = {
            "artifact_kind": "console_served_state_e2e_matrix",
            "schema_version": 1,
            "providers": {report["provider"]: report for report in reports},
            "verdict": verdict,
            "verified": verified,
            "unavailable": [report["provider"] for report in reports if report["verdict"] == "unavailable"],
            "errored": [report["provider"] for report in reports if report["verdict"] == "error"],
        }

    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)

    if args.drop_terminal:
        # Red is the pass. Green means a terminal was dropped on the real path
        # and nothing noticed, which is the incident.
        detected = payload["verdict"] == "red"
        print(
            f"acceptance: dropped terminal -> verdict={payload['verdict']} "
            f"({'detected' if detected else 'NOT DETECTED'})"
        )
        return 0 if detected else 1
    return 0 if payload["verdict"] == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
