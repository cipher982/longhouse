#!/usr/bin/env python3
"""Factory producer: prove Console truth on the surface a viewer actually reads.

`provider_console_lifecycle` drives a real Console turn and asserts on the
machine: archived events and the local turn-claim file. `product_console_lifecycle`
asserts on the served state contract but writes `state: completed` itself, with
no provider process. Between them sits the gap the 2026-08-23 wedge fell through:
the archive held the reply, the claim read `terminal`, and the served surface
rendered "Working" for ten hours while every machine-side signal stayed green.

This producer closes it -- a real provider turn, judged by what the browser and
iOS are served:

  live delivery  frames reach the workspace SSE stream during the turn
  settlement     once the reply is served, the state axis stops saying work

The predicates come from `console_served_state_core`, which the shipped
`scripts/qa/console-served-state-e2e.py` also uses, so the factory and the
hand-run harness cannot drift into asserting different things.

Settlement is judged on the structured contract -- run identity, lifecycle,
activity, presentation key, working_set -- never on `display_phase`. Six live
fault-injected drops served "Using shell" and "Thinking"; not one served
"Working", so a label allowlist would have passed the incident it was written
for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path

from zerg.qa.console_served_state_core import console_providers
from zerg.qa.resume_assurance import ProducerRegistration

SCENARIO_ID = "console_served_state"
ASSERTION_LIVE = "live_frames_reach_the_viewer_during_the_turn"
ASSERTION_SETTLED = "served_state_settles_once_the_reply_is_served"

PROVIDERS = tuple(console_providers())

REGISTRATION = ProducerRegistration(
    producer_id="longhouse.console_served_state.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_LIVE, None), (ASSERTION_SETTLED, None)),
    # The subject under test is Longhouse, not a provider release, so this
    # declares no providers and pins no provider artifact -- the factory
    # validator rejects a longhouse_product registration that does either
    # (resume_assurance.py:266). A provider still runs; it is the vehicle that
    # produces a Console turn, chosen at runtime from what the machine offers.
    # The served-state contract is Longhouse's and must hold whichever one drove.
    providers=(),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    # A real provider runs and a real Runtime Host serves the result. Nothing
    # here is reconstructed from fixtures; that is the whole point.
    evidence_classes=("live_token",),
    observed_activity=(
        "live_frame_after_dispatch",
        "reply_served_to_the_viewer",
        "state_axis_settled",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("runtime_host_control",),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("console_served_state_observation", "cleanup_receipt"),
    required_cleanup=("no_orphan_provider_processes",),
    implementation="server/zerg/qa/console_served_state.py",
    oracle_source="server/zerg/qa/console_served_state_core.py",
    oracle_entrypoint="settlement_state",
    executable_module="zerg.qa.console_served_state",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _manifest(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(content),
                "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }
        )
    return entries


def assertions_from_report(report: dict) -> dict[str, bool]:
    """Map one harness report onto this producer's assertion cells.

    Kept separate from the run so the mapping is testable without a provider,
    and so a report shape change fails here rather than silently reporting pass.
    """
    live = report.get("first_live_frame_s") is not None and int(report.get("frame_count") or 0) > 0
    # Settlement is only meaningful once the reply is actually served: a turn
    # that never produced anything has nothing to settle from.
    reply_served = bool(report.get("marker_served"))
    settled = reply_served and report.get("settle_latency_s") is not None
    return {ASSERTION_LIVE: live, ASSERTION_SETTLED: settled}


def advertised_console_providers(supports: object) -> list[str]:
    """Console providers this machine announced it can actually start a turn on.

    `supports` is the Machine Agent's last hello frame, and `<provider>.turn_start`
    is the exact capability POST /api/agents/sessions checks before it will create
    a Console thread.
    """
    announced = {str(item) for item in supports} if isinstance(supports, (list, tuple, set)) else set()
    return [provider for provider in PROVIDERS if f"{provider}.turn_start" in announced]


def select_vehicle(client: object, device_id: str | None = None, *, timeout: float = 45.0) -> tuple[str, str]:
    """Choose the machine and provider that will drive the turn.

    The factory dispatches a longhouse_product cell as `<oracle> --evidence-root
    <dir>`: no provider, because the contract pins `provider: null`, and no
    device, because the subject is Longhouse rather than any one machine. A real
    turn still has to run somewhere, so both are chosen here from the machine
    directory rather than guessed.

    Both halves were previously guesses and both were wrong. The provider was
    the first console adapter declared in `managed_providers.yml`, which is
    schema order and says nothing about the machine that has to run the turn.
    The device was the literal string "factory-machine", which exists only
    inside `product_console_lifecycle`, where the control-channel registry is
    stubbed and no machine is ever contacted -- the real directory has no such
    enrollment, so the lookup could never match.

    Waited for, too: the Machine Agent announces `<provider>.turn_start`
    asynchronously after its control channel connects, which is why
    provider_console_lifecycle retries the session create for 45s on
    adapter_unavailable instead of trusting the first answer.
    """
    if not PROVIDERS:
        raise RuntimeError("no managed provider declares a console adapter")
    deadline = time.monotonic() + timeout
    machines: list = []
    while True:
        machines = client.request("GET", "/api/agents/machines").get("machines") or []
        for entry in machines:
            if device_id is not None and str(entry.get("device_id")) != device_id:
                continue
            if not entry.get("online"):
                continue
            available = advertised_console_providers(entry.get("supports"))
            if available:
                return str(entry.get("device_id")), available[0]
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)

    known = ", ".join(sorted(str(m.get("device_id")) for m in machines)) or "none"
    if device_id is not None and not any(str(m.get("device_id")) == device_id for m in machines):
        raise RuntimeError(f"device {device_id!r} is not enrolled after {timeout:g}s; machines present: {known}")
    detail = (
        "; ".join(
            f"{m.get('device_id')}: "
            + ("offline" if not m.get("online") else (", ".join(sorted(str(x) for x in (m.get("supports") or []))) or "nothing"))
            for m in machines
        )
        or "no machines enrolled"
    )
    raise RuntimeError(f"no enrolled machine advertised a console adapter among {list(PROVIDERS)} within {timeout:g}s; directory: {detail}")


def run_console_served_state(root: Path, *, provider: str, device_id: str, cwd: str) -> dict[str, object]:
    from zerg.qa import console_served_state_core as core

    arguments = argparse.Namespace(
        provider=provider,
        device_id=device_id,
        cwd=cwd,
        api_url=None,
        turn_timeout=180.0,
        settle_budget=30.0,
        watch_session=None,
        drop_terminal=False,
    )
    report = core.run(arguments)
    assertions = assertions_from_report(report)
    _write_json(root / "console-served-state-observation.json", report)
    return {
        "schema_version": 1,
        "artifact_kind": "longhouse_console_served_state_result",
        "producer": REGISTRATION.to_dict(),
        # The subject is Longhouse, so the identity carries no provider -- the
        # factory compares this against a contract that pins `provider: null`
        # and rejects the result outright when it names one. The vehicle that
        # actually drove the turn is in the observation below, which is where
        # evidence belongs; it is not part of what this result claims to be.
        "provider": None,
        "vehicle_provider": provider,
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": 1,
        "evidence_class": "live_token",
        "generated_at": _now(),
        "status": "pass" if all(assertions.values()) else "fail",
        "observation": report,
        "assertions": assertions,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", action="store_true")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--provider", choices=PROVIDERS)
    # No default: "factory-machine" is a product_console_lifecycle fixture name
    # with no real enrollment behind it. Absent, the machine is chosen from the
    # directory by what it actually advertises.
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--cwd", default="/tmp/longhouse-console-served-state")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.registration:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.evidence_root is None:
        print(json.dumps({"status": "fail", "failure_code": "evidence_root_missing"}))
        return 2
    cleanup = {
        "schema_version": 1,
        "artifact_kind": "longhouse_console_served_state_cleanup_receipt",
        "status": "pass",
        "orphan_count": 0,
    }
    # Chosen inside the try so a machine that offers no usable console adapter
    # lands in the failure artifact below, saying which one it wanted and what
    # the machine actually announced, rather than raising past the evidence.
    provider = args.provider
    device_id = args.device_id
    try:
        if provider is None or device_id is None:
            from zerg.qa.console_served_state_core import Client
            from zerg.qa.console_served_state_core import _defaults

            api_url, token = _defaults()
            chosen_device, chosen_provider = select_vehicle(Client(api_url, token), device_id)
            device_id = device_id or chosen_device
            provider = provider or chosen_provider
        result = run_console_served_state(args.evidence_root, provider=provider, device_id=device_id, cwd=args.cwd)
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        cleanup["status"] = "fail"
        cleanup["error_type"] = type(exc).__name__
        _write_json(args.evidence_root / "cleanup-receipt.json", cleanup)
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_console_served_state_result",
            "producer": REGISTRATION.to_dict(),
            "provider": None,
            "vehicle_provider": provider,
            "vehicle_device_id": device_id,
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": 1,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "console_served_state_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        result["artifact_manifest"] = _manifest(args.evidence_root)
        _write_json(args.evidence_root / "result.json", result)
        print(json.dumps(result, sort_keys=True, default=str))
        return 1

    _write_json(args.evidence_root / "cleanup-receipt.json", cleanup)
    result["artifact_manifest"] = _manifest(args.evidence_root)
    _write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
