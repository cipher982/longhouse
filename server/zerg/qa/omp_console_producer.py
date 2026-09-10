#!/usr/bin/env python3
"""Live OMP Console qualification through the native ``omp_print`` adapter."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

SCENARIO_ID = "omp_console_lifecycle"
ASSERTION_ID = "omp_console_turn_settled_and_bound"
SUPPORTED_VARIANT = lifecycle.SUPPORTED_VARIANT

REGISTRATION = ProducerRegistration(
    producer_id="omp.console_lifecycle.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, None),),
    providers=("omp",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("live_token",),
    observed_activity=(
        "runtime_host_turn_dispatch",
        "omp_agent_end_terminal_observed",
        "omp_stream_drained",
        "omp_native_archive_bound",
        "no_orphan_provider_processes",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("omp_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "adapter_dispatch_receipt",
        "transcript_flush_receipt",
        "console_boundary_receipt",
        "provider_response_binding_receipt",
        "interrupt_contract_receipt",
        "cleanup_receipt",
        "omp_settlement_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "no_orphan_provider_processes"),
    implementation="server/zerg/qa/omp_console_producer.py",
    oracle_source="server/zerg/qa/omp_console_producer.py",
    oracle_entrypoint="omp_console_assertions",
    executable_module="zerg.qa.omp_console_producer",
    provider_artifact_required=True,
)
_VARIANT = execution_variant_key(provider="omp", assertion_id=ASSERTION_ID, scenario_id=SCENARIO_ID, variant=None)
PROFILE = "omp_print_v1"
_PROFILE = identity.IdentityProfile(
    provider="omp",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(),
    oracle_source=Path(__file__),
)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _native_settlement(root: Path) -> dict[str, object]:
    """Read OMP's own terminal event and archive, never Pi's settlement marker."""

    retention = _read_json(root / "provider-source-retention.json") or {}
    sources = retention.get("sources") if isinstance(retention.get("sources"), list) else []
    agent_end_terminal = False
    agent_settled_seen = False
    native_archive_bound = False
    source_paths: list[str] = []
    for item in sources:
        if not isinstance(item, Mapping) or item.get("retained") is not True:
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        path = root / raw_path
        source_paths.append(raw_path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping):
                continue
            if event.get("type") == "session" and isinstance(event.get("id"), str) and event.get("id"):
                native_archive_bound = True
            if event.get("type") == "agent_settled":
                agent_settled_seen = True
            if event.get("type") == "agent_end":
                is_terminal = event.get("isTerminal") is True or event.get("willContinue") is False
                agent_end_terminal = agent_end_terminal or is_terminal
    boundary = _read_json(root / "console-boundary-receipt.json") or {}
    flush = _read_json(root / "transcript-flush-receipt.json") or {}
    stream_drained = (
        boundary.get("claim_state") == "terminal"
        and boundary.get("claim_terminal_state") == "run_completed"
        and flush.get("status") == "pass"
        and flush.get("exit_code") == 0
    )
    return {
        "status": "pass" if agent_end_terminal and stream_drained and native_archive_bound else "fail",
        "agent_end_terminal": agent_end_terminal,
        "agent_settled_seen": agent_settled_seen,
        "agent_settled_is_not_completion_contract": True,
        "stream_drained": stream_drained,
        "native_archive_bound": native_archive_bound,
        "source_paths": source_paths,
    }


def omp_console_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    settlement = observation.get("omp_settlement")
    settlement = settlement if isinstance(settlement, Mapping) else {}
    return {
        ASSERTION_ID: all(
            (
                observation.get("runtime_host_turn_dispatch") is True,
                settlement.get("agent_end_terminal") is True,
                settlement.get("stream_drained") is True,
                settlement.get("native_archive_bound") is True,
                observation.get("no_orphan_provider_processes") is True,
            )
        )
    }


def run_omp_console(args: argparse.Namespace) -> dict[str, object]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    generic = lifecycle._run_live("omp", SUPPORTED_VARIANT, args, root)
    observation = dict(generic.get("observation") or {})
    observation["runtime_host_turn_dispatch"] = observation.get("adapter_dispatch_started") is True
    settlement = _native_settlement(root)
    observation["omp_settlement"] = settlement
    observation.update(
        {
            "omp_agent_end_terminal_observed": settlement.get("agent_end_terminal") is True,
            "omp_stream_drained": settlement.get("stream_drained") is True,
            "omp_native_archive_bound": settlement.get("native_archive_bound") is True,
        }
    )
    lifecycle.write_json(root / "omp-settlement-receipt.json", settlement)
    assertions = omp_console_assertions(observation)
    result = {
        "schema_version": 1,
        "artifact_kind": "omp_console_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "omp",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        "status": "pass" if generic.get("status") == "pass" and all(assertions.values()) else "fail",
        "assertions": assertions,
        "provider_binary": generic.get("provider_binary"),
        "observation": observation,
        "generic_lifecycle_result": generic,
        "artifact_manifest": artifact_manifest(root),
    }
    lifecycle.write_json(root / "result.json", result)
    return result


def _request_args(request_path: Path, output_root: Path) -> argparse.Namespace:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("OMP qualification request must be an object")
    engine = Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or "")
    cli = Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse")
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        engine=engine,
        longhouse_cli=cli,
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        provider="omp",
        variant=SUPPORTED_VARIANT,
        model=os.environ.get("LONGHOUSE_OMP_QUALIFICATION_MODEL", ""),
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    identity.load_request(
        request_path,
        provider="omp",
        profile=PROFILE,
        version_grammar=_PROFILE.version_grammar,
    )

    def execute(binary: Path, evidence_root: Path):
        args = _request_args(request_path, evidence_root)
        args.provider_bin = binary
        result = run_omp_console(args)
        observation = dict(result.get("observation") or {})
        passed = result.get("status") == "pass"
        assertion = semantic.SemanticAssertion(
            ASSERTION_ID,
            AssertionOutcome.PASS if passed else AssertionOutcome.SEMANTIC_FAIL,
            EvidenceClass.LIVE_TOKEN,
        )
        return (
            observation,
            (assertion,),
            tuple(value for name in ("OPENROUTER_API_KEY",) if (value := str(os.environ.get(name) or "").strip())),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=(ASSERTION_ID,),
        executor=execute,
        oracle_source=Path(__file__),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=(_VARIANT,))
    parser.add_argument("--model", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    try:
        result = run_omp_console(args)
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        result = {
            "schema_version": 1,
            "artifact_kind": "omp_console_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "omp",
            "variant": SUPPORTED_VARIANT,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "omp_console_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lifecycle.write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
