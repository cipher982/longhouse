#!/usr/bin/env python3
"""Two-process Pi Console proof with a real native tool call/result pair."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

SCENARIO_ID = "pi_console_tool_lifecycle"
ASSERTION_ID = "pi_console_tool_enabled"
SUPPORTED_VARIANT = lifecycle.SUPPORTED_VARIANT
PROFILE = "pi_console_tool_v1"
_PROFILE = identity.IdentityProfile(
    provider="pi",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(),
    oracle_source=Path(__file__),
)
REGISTRATION = ProducerRegistration(
    producer_id="pi.console_tool.v1",
    producer_revision=5,
    scenario_id=SCENARIO_ID,
    # 5: the shared lifecycle adds a mid-turn steer step (its own cell).
    # 7: the native reply is the row that answers with the marker, not every
    # row that names it.
    # 22: the waits for a served reply read the newest events, as the steer receipt does.
    # 23: a process that has exited counts as stopped before its parent collects it (live_kit.pid_dead), and the
    # shared steer step asks for its command in the foreground.
    scenario_revision=23,
    assertion_cells=((ASSERTION_ID, None), (lifecycle.STEER_ASSERTION_ID, None)),
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
        "console_boundary_receipt",
        "interrupt_contract_receipt",
        "console_continuation_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "no_orphan_provider_processes", "canary_session_hidden"),
    implementation="server/zerg/qa/pi_console_tool_producer.py",
    oracle_source="server/zerg/qa/pi_console_tool_producer.py",
    oracle_entrypoint="pi_console_tool_assertions",
    executable_module="zerg.qa.pi_console_tool_producer",
)
_VARIANT = execution_variant_key(provider="pi", assertion_id=ASSERTION_ID, scenario_id=SCENARIO_ID, variant=None)
# The steer cell is its own factory execution (one per cell).
_STEER_VARIANT = lifecycle.steer_execution_variant("pi", SCENARIO_ID)


def pi_console_tool_assertions(observation: dict[str, object]) -> dict[str, bool]:
    generic = lifecycle.console_lifecycle_assertions(observation).get(lifecycle.ASSERTION_ID) is True
    return {
        ASSERTION_ID: generic and observation.get("pi_tool_enabled") is True and observation.get("continuation_context_recalled") is True
    }


def run_pi_console_tool(args: argparse.Namespace, *, raises: bool = False) -> dict[str, object]:
    # Reuse the established Runtime Host + Machine Agent + stock-provider
    # transaction. Only the subject registration and Pi-specific oracle differ.
    steer = getattr(args, "variant", None) == _STEER_VARIANT

    def body(run: Run) -> None:
        generic = lifecycle._run_live("pi", lifecycle.SUPPORTED_VARIANT, args, run.root, steer=steer)
        run.observation = dict(generic["observation"])
        run.assertions = generic["assertions"] if steer else pi_console_tool_assertions(run.observation)
        run.fields.update(steer=generic["steer"], provider_binary=generic["provider_binary"])

    return lifecycle.run_console_cell(PRODUCER, args, body, raises=raises)


def _request_args(request_path: Path, output_root: Path) -> argparse.Namespace:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("Pi Console qualification request must be an object")
    return argparse.Namespace(
        evidence_root=output_root,
        repo_root=Path(__file__).resolve().parents[3],
        engine=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or ""),
        longhouse_cli=Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse"),
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        provider="pi",
        variant=SUPPORTED_VARIANT,
        model=os.environ.get("LONGHOUSE_PI_QUALIFICATION_MODEL", ""),
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    identity.load_request(
        request_path,
        provider="pi",
        profile=PROFILE,
        version_grammar=_PROFILE.version_grammar,
    )

    def execute(binary: Path, evidence_root: Path):
        args = _request_args(request_path, evidence_root)
        args.provider_bin = binary
        result = run_pi_console_tool(args, raises=True)
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
            tuple(
                value
                for value in (
                    str(os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN") or "").strip(),
                    str(os.environ.get("OPENROUTER_API_KEY") or "").strip(),
                )
                if value
            ),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=(ASSERTION_ID,),
        executor=execute,
        oracle_source=Path(__file__),
        scenario_revision=REGISTRATION.scenario_revision,
    )


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="pi_console_tool_lifecycle_result",
    failure_code="pi_console_tool_lifecycle_failed",
    needs=("evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin", "provider_version", "model"),
    variants=(_VARIANT, _STEER_VARIANT),
    negative_controls=(lifecycle.CONSOLE_STEER_FAULT,),
    refuse=lifecycle.negative_control_needs_steer((_STEER_VARIANT,)),
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_pi_console_tool, argv)


if __name__ == "__main__":
    raise SystemExit(main())
