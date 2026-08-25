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
from zerg.qa.console_served_state_core import Client  # noqa: E402
from zerg.qa.console_served_state_core import _defaults  # noqa: E402
from zerg.qa.console_served_state_core import console_providers  # noqa: E402
from zerg.qa.console_served_state_core import run  # noqa: E402


def connected_machine_targets() -> list[tuple[str, list[str]]]:
    """Every connected machine and the Console providers it actually offers.

    Checking one hardcoded machine is how a second one rots unnoticed. The
    always-on box sat with a dead Claude credential while this check stayed
    green, because the check only ever asked the laptop -- and an always-on box
    is precisely the machine nobody is sitting in front of to notice.

    Each machine is asked only about providers it advertises, so a box without
    a given CLI installed is never treated as a failure for lacking it.
    """

    api_url, token = _defaults()
    if not api_url or not token:
        raise RuntimeError("Longhouse API URL and device token are required")
    client = Client(api_url.rstrip("/"), token)
    directory = client.request("GET", "/api/agents/machines")
    targets: list[tuple[str, list[str]]] = []
    for machine in directory.get("machines") or []:
        if not machine.get("online"):
            continue
        offered = [
            str(option.get("provider"))
            for option in (machine.get("launch") or {}).get("providers") or []
            if option.get("provider")
        ]
        if offered:
            targets.append((str(machine.get("device_id")), sorted(offered)))
    return targets


def machine_workspace(client: Client, device_id: str) -> str | None:
    """A directory that exists on *that* machine, from its own suggestions.

    One `--cwd` cannot serve several machines: a laptop path handed to a Linux
    box fails for a reason that has nothing to do with Console, and would make
    the whole check red for the wrong cause. Each machine already reports the
    workspaces it has actually been used in, so ask it.
    """

    try:
        payload = client.request("GET", f"/api/agents/machines/{device_id}/workspaces?limit=1")
    except Exception:
        return None
    for entry in payload.get("workspaces") or []:
        path = str(entry.get("path") or "").strip()
        if path.startswith("/"):
            return path
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        default="codex",
        help="a provider name, or 'all' to derive the set from schemas/managed_providers.yml",
    )
    parser.add_argument(
        "--device-id",
        default=os.environ.get("LONGHOUSE_DEVICE_ID") or "cinder",
        help="a device id, or 'all' to prove every connected machine against the providers it offers",
    )
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

    if args.device_id == "all":
        # Ask each machine only about what it offers, so a box missing a CLI is
        # not reported as a failure for missing it.
        targets = connected_machine_targets()
        if args.provider != "all":
            targets = [(device, [args.provider]) for device, offered in targets if args.provider in offered]
    else:
        providers = console_providers() if args.provider == "all" else [args.provider]
        targets = [(args.device_id, providers)]

    cwd_by_device: dict[str, str] = {}
    if args.device_id == "all":
        api_url, token = _defaults()
        client = Client((args.api_url or api_url or "").rstrip("/"), token)
        for device, _ in targets:
            resolved = machine_workspace(client, device)
            if resolved:
                cwd_by_device[device] = resolved

    attempts = [(device, provider) for device, providers in targets for provider in providers]

    reports: list[dict] = []
    for device_id, provider in attempts:
        attempt = argparse.Namespace(
            **{
                **vars(args),
                "provider": provider,
                "device_id": device_id,
                "cwd": cwd_by_device.get(device_id, args.cwd),
            }
        )
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
                    "device_id": device_id,
                    # The API failing is not a verdict about this provider. Say
                    # so separately, and still count it: a check that could not
                    # run is not a check that passed.
                    "verdict": "unavailable" if unavailable else "error",
                    "failures": [str(error)],
                }
            )
        except RuntimeError as error:
            # This used to record "unavailable" on the theory that a provider
            # which will not start is a provider install rather than a
            # Longhouse failure. That theory was wrong about which errors reach
            # here: the genuine not-offered case is `adapter_unavailable`,
            # caught above, and every RuntimeError this harness raises is a
            # real failure -- a refused turn, a turn with no run, a stream that
            # died, missing credentials.
            #
            # The cost of the mistake was measured. A Console turn dispatched
            # to cube came back `state: failed`, was classified unavailable,
            # and so did not turn the daily check red. The machine advertised
            # Claude the whole time. A capability the product offers and cannot
            # deliver is exactly what this check exists to catch.
            reports.append(
                {
                    "artifact_kind": "console_served_state_e2e",
                    "schema_version": 1,
                    "provider": provider,
                    "device_id": device_id,
                    "verdict": "error",
                    "failures": [str(error)],
                }
            )

    if len(reports) == 1 and args.device_id != "all":
        payload = reports[0]
    else:
        def label(report: dict) -> str:
            return f"{report.get('device_id')}/{report['provider']}"

        verified = [label(report) for report in reports if report["verdict"] == "green"]
        failed = [label(report) for report in reports if report["verdict"] in {"red", "error"}]
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
            "providers": {f"{report.get('device_id')}/{report['provider']}": report for report in reports},
            "verdict": verdict,
            "verified": verified,
            "unavailable": [label(report) for report in reports if report["verdict"] == "unavailable"],
            "errored": [label(report) for report in reports if report["verdict"] == "error"],
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
