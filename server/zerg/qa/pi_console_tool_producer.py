#!/usr/bin/env python3
"""Two-process Pi Console proof with a real native tool call/result pair."""

from __future__ import annotations

import argparse
import json
import sys

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "pi_console_tool_lifecycle"
ASSERTION_ID = "pi_console_tool_enabled"
REGISTRATION = ProducerRegistration(
    producer_id="pi.console_tool.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, None),),
    providers=("pi",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("live_token",),
    observed_activity=(
        "runtime_host_turn_dispatch",
        "stock_pi_print_tool_call",
        "stock_pi_print_tool_result",
        "native_tool_call_result_linked",
        "durable_assistant_response_bound",
        "no_orphan_provider_processes",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("pi_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "adapter_dispatch_receipt",
        "provider_response_binding_receipt",
        "native_tool_receipt",
        "transcript_flush_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "no_orphan_provider_processes"),
    implementation="server/zerg/qa/pi_console_tool_producer.py",
    oracle_source="server/zerg/qa/pi_console_tool_producer.py",
    oracle_entrypoint="pi_console_tool_assertions",
    executable_module="zerg.qa.pi_console_tool_producer",
)
_VARIANT = execution_variant_key(provider="pi", assertion_id=ASSERTION_ID, scenario_id=SCENARIO_ID, variant=None)


def pi_console_tool_assertions(observation: dict[str, object]) -> dict[str, bool]:
    generic = lifecycle.console_lifecycle_assertions(observation).get(lifecycle.ASSERTION_ID) is True
    return {ASSERTION_ID: generic and observation.get("pi_tool_enabled") is True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=(_VARIANT,))
    parser.add_argument("--model", required=True)
    return parser


def run_pi_console_tool(args: argparse.Namespace) -> dict[str, object]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Reuse the established Runtime Host + Machine Agent + stock-provider
    # transaction. Only the subject registration and Pi-specific oracle differ.
    generic = lifecycle._run_live("pi", lifecycle.SUPPORTED_VARIANT, args, root)
    observation = dict(generic.get("observation") or {})
    assertions = pi_console_tool_assertions(observation)
    result = {
        "schema_version": 1,
        "artifact_kind": "pi_console_tool_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "pi",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        "status": "pass" if generic.get("status") == "pass" and all(assertions.values()) else "fail",
        "assertions": assertions,
        "observation": observation,
        "generic_lifecycle_result": generic,
        "artifact_manifest": artifact_manifest(root),
    }
    lifecycle.write_json(root / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    missing = [
        name
        for name in ("evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin", "provider_version")
        if getattr(args, name) is None
    ]
    if missing:
        print(json.dumps({"status": "fail", "failure_code": f"missing_required_argument:--{missing[0].replace('_', '-')}"}))
        return 2
    try:
        result = run_pi_console_tool(args)
    except Exception as exc:  # noqa: BLE001 - keep a typed failure artifact
        result = {
            "schema_version": 1,
            "artifact_kind": "pi_console_tool_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "pi",
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "pi_console_tool_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.evidence_root:
            args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            lifecycle.write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
