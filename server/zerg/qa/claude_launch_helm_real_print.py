#!/usr/bin/env python3
"""Continuous producer for claude/session.launch.helm.

Proves ``claude_cli_channel_contract_preserved`` for the ``claude_real_print``
scenario using the exact-binary, no-token contract check
``claude_real_print_qualification.py`` already runs for this same assertion
id: ``provider_live_canary.run_provider_live_canary`` spawns the real stock
``claude`` binary with an isolated, credential-stripped ``$HOME`` and checks
its binary/auth-prompt/channel/PTY contract without ever touching a live
model. That is exactly what ``launch_local``'s ``operation_evidence`` entry in
``schemas/managed_providers.yml`` already names as the source for this
capability, so this producer calls it directly rather than re-deriving it.

``session.turn.start``'s ``real_print_marker_returned`` assertion shares this
same scenario_id but requires ``live_token`` evidence (an actual model
response). See ``claude_turn_start_real_print.py`` for that half -- kept in a
separate producer/registration deliberately; see this repo's factory-work
report for why one combined registration does not fit the resume-assurance
compiler's per-cell evidence-class contract here.
"""

from __future__ import annotations

import argparse
from typing import Any

from zerg.qa.claude_live_session_support import sha256_file
from zerg.qa.claude_live_session_support import write_claude_cleanup_aggregate
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_live_canary import run_provider_live_canary
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

_SCENARIO_ID = "claude_real_print"
_ASSERTION_ID = "claude_cli_channel_contract_preserved"
# No `variant:` key is authored for this assertion in
# schemas/managed_providers.yml, so the compiler always passes this generated
# `cell:<provider>:<assertion_id>:<scenario_id>` execution_variant as
# --variant -- see resume_assurance.execution_variant_key and
# codex_turn_boundary_native.py, which established this exact pattern.
_EXECUTION_VARIANT = execution_variant_key(
    provider="claude",
    assertion_id=_ASSERTION_ID,
    scenario_id=_SCENARIO_ID,
    variant=None,
)

REGISTRATION = ProducerRegistration(
    producer_id="claude.launch_helm_real_print.v1",
    producer_revision=3,
    scenario_id=_SCENARIO_ID,
    # 3: a failure after the binary was identified carries its receipt.
    scenario_revision=3,
    assertion_cells=((_ASSERTION_ID, None),),
    providers=("claude",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_no_token",),
    observed_activity=("claude_binary_auth_channel_pty_contract_checked",),
    acquisition_methods=("staged_release", "observed_install"),
    # Deliberately empty: this scenario's entire point is exercising the real
    # claude binary in an isolated, credential-stripped $HOME. Declaring a
    # provider-token credential binding here would misstate what this producer
    # actually uses.
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("provider_binary_receipt", "provider_live_canary_artifact", "cleanup_receipt"),
    required_cleanup=("provider_live_canary_processes_exited",),
    implementation="server/zerg/qa/claude_launch_helm_real_print.py",
    oracle_source="server/zerg/qa/provider_release_semantic_oracles.py",
    # provider_release_semantic_oracles.py only declares the assertion-id set
    # for this scenario (assertions_for); it has no per-observation judge
    # function, unlike provider_resume_oracles.native_resume_assertions. That
    # is the same shape provider_generic_resume.py already uses for the same
    # reason (see its own oracle_entrypoint="assertions_for"), and it is what
    # claude_real_print_qualification.py -- the scenario's existing real
    # executor -- also declares as its oracle_source for this exact scenario.
    oracle_entrypoint="assertions_for",
    executable_module="zerg.qa.claude_launch_helm_real_print",
)

_ARTIFACT_KIND = "claude_launch_helm_real_print_result"


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    try:
        provider_receipt = {"path": str(args.provider_bin), "sha256": sha256_file(args.provider_bin)}
        write_json(root / "provider-binary-receipt.json", provider_receipt)
        run.fields["provider_binary"] = provider_receipt

        canary_root = root / "no-token-canary"
        artifact_path = canary_root / "provider-live-canary.json"
        canary = run_provider_live_canary(
            {
                "provider": "claude",
                "provider_bin": str(args.provider_bin),
                "artifact": artifact_path,
                "evidence_root": canary_root,
                "wait_ready_secs": args.wait_ready_secs,
                "json": False,
            }
        )
        write_json(
            root / "provider-live-canary-artifact.json",
            {
                "schema_version": 1,
                "artifact_kind": "claude_provider_live_canary_artifact_receipt",
                "source_artifact": {
                    "path": artifact_path.relative_to(root).as_posix(),
                    "size": artifact_path.stat().st_size,
                    "sha256": sha256_file(artifact_path),
                },
            },
        )
        verdict = str(canary.get("verdict") or "red")
        contract_preserved = verdict == "green"

        run.observation = {
            "verdict": verdict,
            "failure_code": canary.get("failure_code"),
            "recommendation": canary.get("recommendation"),
            "provider_version": canary.get("provider_version"),
            "canaries": canary.get("canaries"),
            "operation_evidence": canary.get("operation_evidence"),
            "artifact_path": str(artifact_path),
        }
        run.assertions = {_ASSERTION_ID: contract_preserved}
        write_claude_cleanup_aggregate(
            root,
            producer_id=REGISTRATION.producer_id,
            required_cleanup=REGISTRATION.required_cleanup,
            outcomes={"provider_live_canary_processes_exited": True},
            diagnostics={"execution": "synchronous provider-live canary returned without an owned process handle"},
        )
    except Exception:  # noqa: BLE001 - record the cleanup, then let the shell type the failure
        cleanup_recording_error = None
        try:
            write_claude_cleanup_aggregate(
                root,
                producer_id=REGISTRATION.producer_id,
                required_cleanup=REGISTRATION.required_cleanup,
                outcomes={"provider_live_canary_processes_exited": True},
                diagnostics={"execution": "synchronous provider-live canary exited through its bounded call"},
            )
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error below
            cleanup_recording_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        if cleanup_recording_error:
            run.fields["cleanup_recording_error"] = cleanup_recording_error
        raise


def run_launch_helm_scenario(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args))


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind=_ARTIFACT_KIND,
    failure_code="claude_launch_helm_real_print_failed",
    needs=("evidence_root", "provider_bin"),
    binaries=(("provider_bin", "claude_binary_missing"),),
    variants=(_EXECUTION_VARIANT,),
    provider_bin_aliases=("--claude-bin",),
    add_arguments=lambda parser: parser.add_argument("--wait-ready-secs", type=float, default=15.0),
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_launch_helm_scenario, argv)


if __name__ == "__main__":
    raise SystemExit(main())
