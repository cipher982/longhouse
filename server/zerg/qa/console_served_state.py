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
        "provider": provider,
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
    parser.add_argument("--device-id", default="factory-machine")
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
    if args.provider is None:
        print(json.dumps({"status": "fail", "failure_code": "provider_missing"}))
        return 2

    cleanup = {
        "schema_version": 1,
        "artifact_kind": "longhouse_console_served_state_cleanup_receipt",
        "status": "pass",
        "orphan_count": 0,
    }
    try:
        result = run_console_served_state(args.evidence_root, provider=args.provider, device_id=args.device_id, cwd=args.cwd)
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        cleanup["status"] = "fail"
        cleanup["error_type"] = type(exc).__name__
        _write_json(args.evidence_root / "cleanup-receipt.json", cleanup)
        result = {
            "schema_version": 1,
            "artifact_kind": "longhouse_console_served_state_result",
            "producer": REGISTRATION.to_dict(),
            "provider": args.provider,
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
