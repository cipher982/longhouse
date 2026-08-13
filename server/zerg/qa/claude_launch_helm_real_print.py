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
import json
import os
import sys
from pathlib import Path
from typing import Any

from zerg.qa.claude_live_session_support import artifact_manifest
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.claude_live_session_support import sha256_file
from zerg.qa.claude_live_session_support import write_json
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
    producer_revision=1,
    scenario_id=_SCENARIO_ID,
    scenario_revision=1,
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
    required_artifacts=("provider_binary_receipt", "provider_live_canary_artifact"),
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


def run_launch_helm_scenario(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        provider_receipt = {"path": str(args.claude_bin), "sha256": sha256_file(args.claude_bin)}
        write_json(root / "provider-binary-receipt.json", provider_receipt)

        canary_root = root / "no-token-canary"
        artifact_path = canary_root / "provider-live-canary.json"
        canary = run_provider_live_canary(
            {
                "provider": "claude",
                "provider_bin": str(args.claude_bin),
                "artifact": artifact_path,
                "evidence_root": canary_root,
                "wait_ready_secs": args.wait_ready_secs,
                "json": False,
            }
        )
        verdict = str(canary.get("verdict") or "red")
        contract_preserved = verdict == "green"

        observation = {
            "verdict": verdict,
            "failure_code": canary.get("failure_code"),
            "recommendation": canary.get("recommendation"),
            "provider_version": canary.get("provider_version"),
            "canaries": canary.get("canaries"),
            "operation_evidence": canary.get("operation_evidence"),
            "artifact_path": str(artifact_path),
        }
        assertions = {_ASSERTION_ID: contract_preserved}
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": _ARTIFACT_KIND,
            "producer": REGISTRATION.to_dict(),
            "provider": "claude",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_no_token",
            "generated_at": now_iso(),
            "status": "pass" if contract_preserved else "fail",
            "observation": observation,
            "assertions": assertions,
            "provider_binary": provider_receipt,
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = {
            "schema_version": 1,
            "artifact_kind": _ARTIFACT_KIND,
            "producer": REGISTRATION.to_dict(),
            "provider": "claude",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_no_token",
            "generated_at": now_iso(),
            "status": "fail",
            "failure_code": "claude_launch_helm_real_print_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", failure)
        return failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=(_EXECUTION_VARIANT,))
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--claude-bin", type=Path)
    parser.add_argument("--wait-ready-secs", type=float, default=15.0)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    for required in ("evidence_root", "claude_bin"):
        if getattr(args, required) is None:
            print(json.dumps({"status": "fail", "failure_code": f"missing_required_argument:--{required.replace('_', '-')}"}))
            return 2
    if not args.claude_bin.is_file() or not os.access(args.claude_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "claude_binary_missing"}))
        return 2
    result = run_launch_helm_scenario(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
