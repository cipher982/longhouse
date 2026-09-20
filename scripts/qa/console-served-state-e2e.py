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
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from zerg.qa.console_served_state_core import ApiError  # noqa: E402
from zerg.qa.console_served_state_core import Client  # noqa: E402
from zerg.qa.console_served_state_core import _defaults  # noqa: E402
from zerg.qa.console_served_state_core import console_providers  # noqa: E402
from zerg.qa.console_served_state_core import run  # noqa: E402
from zerg.qa.live_session_toolkit import require_disposable_runtime  # noqa: E402
from zerg.qa.live_session_toolkit import start_transcript_shipper  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_PROVIDER_BIN_ENVS = (
    "LONGHOUSE_CODEX_BIN",
    "LONGHOUSE_CLAUDE_BIN",
    "LONGHOUSE_OPENCODE_BIN",
    "LONGHOUSE_ANTIGRAVITY_BIN",
    "LONGHOUSE_CURSOR_BIN",
    "LONGHOUSE_PI_BIN",
    "LONGHOUSE_OMP_BIN",
)
_PROVIDER_MODEL_ENVS = {
    "codex": "CODEX_MODEL",
    "claude": "ANTHROPIC_MODEL",
    "opencode": "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL",
    "cursor": "CURSOR_MODEL",
    "pi": "LONGHOUSE_PI_QUALIFICATION_MODEL",
    "omp": "LONGHOUSE_OMP_QUALIFICATION_MODEL",
}


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
        payload = client.request(
            "GET", f"/api/agents/machines/{device_id}/workspaces?limit=1"
        )
    except Exception:
        return None
    for entry in payload.get("workspaces") or []:
        path = str(entry.get("path") or "").strip()
        if path.startswith("/"):
            return path
    return None


def _build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument("--model", default=None)
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
    parser.add_argument(
        "--owned-machine-agent",
        action="store_true",
        help="start the disposable native Machine Agent and target only its registered machine",
    )
    parser.add_argument("--engine", type=Path, default=None)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.device_id == "all":
        # Ask each machine only about what it offers, so a box missing a CLI is
        # not reported as a failure for missing it.
        targets = connected_machine_targets()
        if args.provider != "all":
            targets = [
                (device, [args.provider])
                for device, offered in targets
                if args.provider in offered
            ]
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

    attempts = [
        (device, provider) for device, providers in targets for provider in providers
    ]

    reports: list[dict] = []
    for device_id, provider in attempts:
        attempt = argparse.Namespace(
            **{
                **vars(args),
                "provider": provider,
                "device_id": device_id,
                "cwd": cwd_by_device.get(device_id, args.cwd),
                "model": args.model
                or os.environ.get(_PROVIDER_MODEL_ENVS.get(provider, ""), ""),
            }
        )
        try:
            report = run(attempt)
            reports.append(report)
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
        failed = [
            label(report) for report in reports if report["verdict"] in {"red", "error"}
        ]
        unavailable = [
            label(report) for report in reports if report["verdict"] == "unavailable"
        ]
        if failed:
            verdict = "red"
        elif unavailable or not verified:
            # An owned fixture must advertise every canonical Console candidate.
            # An unavailable provider is not silently downgraded to green merely
            # because another provider completed a turn.
            verdict = "unqualified"
        else:
            verdict = "green"
        payload = {
            "artifact_kind": "console_served_state_e2e_matrix",
            "schema_version": 1,
            "providers": {
                f"{report.get('device_id')}/{report['provider']}": report
                for report in reports
            },
            "verdict": verdict,
            "verified": verified,
            "unavailable": [
                label(report)
                for report in reports
                if report["verdict"] == "unavailable"
            ],
            "errored": [
                label(report) for report in reports if report["verdict"] == "error"
            ],
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


def _run_owned_fixture(args: argparse.Namespace) -> int:
    api_url = (args.api_url or os.environ.get("LONGHOUSE_API_URL") or "").rstrip("/")
    token = (
        os.environ.get("LONGHOUSE_MACHINE_TOKEN")
        or os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN")
        or ""
    ).strip()
    if not api_url or not token:
        raise RuntimeError(
            "owned Console fixture requires LONGHOUSE_API_URL and LONGHOUSE_MACHINE_TOKEN"
        )
    require_disposable_runtime(api_url)
    if args.engine is None or not args.engine.is_file():
        raise RuntimeError("owned Console fixture requires an existing --engine binary")
    codex_bin = Path(os.environ.get("LONGHOUSE_CODEX_BIN", ""))
    if not codex_bin.is_file():
        raise RuntimeError("owned Console fixture requires LONGHOUSE_CODEX_BIN")
    claude_bin = Path(os.environ.get("LONGHOUSE_CLAUDE_BIN", ""))
    shipper_provider = "claude" if claude_bin.is_file() else "codex"
    shipper_bin = claude_bin if shipper_provider == "claude" else codex_bin
    fixture_root = Path(os.environ.get("CONSOLE_FIXTURE_ROOT", ""))
    if not fixture_root.is_absolute() or not fixture_root.is_dir():
        raise RuntimeError(
            "owned Console fixture requires a staged CONSOLE_FIXTURE_ROOT"
        )
    home = fixture_root / "home"
    if not home.is_dir():
        raise RuntimeError("owned Console fixture requires its staged home directory")
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(home),
            "LONGHOUSE_ENGINE_BIN": str(args.engine),
            "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
            "LONGHOUSE_LAUNCH_ACTOR": "automation",
            "LONGHOUSE_LAUNCH_SURFACE": "test",
            "AGENT_CLI_CREDENTIAL_STORE": "file",
        }
    )
    for name in _PROVIDER_BIN_ENVS:
        value = environment.get(name, "").strip()
        if value and not Path(value).is_file():
            raise RuntimeError(
                f"owned Console fixture provider binary is missing: {name}={value}"
            )
    shipper_args = SimpleNamespace(
        api_url=api_url,
        agents_token=token,
        engine=args.engine,
        provider_bin=shipper_bin,
        repo_root=ROOT,
    )
    shipper = start_transcript_shipper(
        shipper_provider,
        shipper_args,
        home=home,
        environment=environment,
        evidence_root=home / "fixture-evidence",
    )
    args.api_url = api_url
    args.device_id = shipper.machine_name
    try:
        return _run(args)
    finally:
        cleanup = shipper.stop()
        stopped = (
            cleanup.get("process_dead") is True
            and cleanup.get("process_group_dead") is True
        )
        if args.json_out:
            result_path = Path(args.json_out)
            payload = (
                json.loads(result_path.read_text())
                if result_path.is_file()
                else {"verdict": "red"}
            )
            payload["machine_agent_cleanup"] = cleanup
            if not stopped:
                payload["verdict"] = "red"
            result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if not stopped:
            raise RuntimeError("owned Console Machine Agent process group did not stop")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.owned_machine_agent:
        return _run(args)
    previous_handlers = {}

    def stop_on_signal(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, stop_on_signal)
    try:
        return _run_owned_fixture(args)
    except KeyboardInterrupt as error:
        signum = (
            int(error.args[0])
            if error.args and isinstance(error.args[0], int)
            else signal.SIGINT
        )
        return 128 + signum
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
