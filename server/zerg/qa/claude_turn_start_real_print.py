#!/usr/bin/env python3
"""Continuous producer for claude/session.turn.start.

Proves ``real_print_marker_returned`` for the ``claude_real_print`` scenario
by reusing ``claude_real_print_qualification._execute`` -- the real, already
-working executor for this exact assertion id (see
``provider_release_semantic_oracles.ASSERTIONS_BY_SCENARIO["claude_real_print"]``).
``_execute`` runs the no-token contract check first (a live model call is
only attempted once that passes) and then, when explicit credentials are
present, invokes ``run_claude_real_print_canary`` from
``scripts/qa/provider-control-e2e-canary.py`` -- a real ``claude --print``
invocation whose returned marker text is the actual postcondition here.

This producer only opts that live phase in (``LONGHOUSE_CLAUDE_QUALIFICATION_LIVE=1``)
and translates ``_execute``'s ``(observation, assertions, credentials)`` return
shape into the factory's ``result.json`` contract -- including the redaction
step ``_execute``'s own caller (``provider_semantic_qualification.run_semantic_profile``)
normally performs and ``_execute`` itself does not, since this producer calls
``_execute`` directly instead of going through that caller.

``session.launch.helm``'s ``claude_cli_channel_contract_preserved`` assertion
shares this same scenario_id; see ``claude_launch_helm_real_print.py`` for
that half and this repo's factory-work report for why these are two
registrations, not one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from zerg.qa import claude_real_print_qualification as real_print_qual
from zerg.qa.claude_live_session_support import artifact_manifest
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.claude_live_session_support import sha256_file
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.provider_semantic_qualification import _redact_value as redact_semantic_value
from zerg.qa.provider_semantic_qualification import _scrub_tree as scrub_semantic_tree
from zerg.qa.provider_semantic_qualification import temporary_environment
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome

_SCENARIO_ID = "claude_real_print"
_ASSERTION_ID = "real_print_marker_returned"
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
    producer_id="claude.turn_start_real_print.v1",
    producer_revision=1,
    scenario_id=_SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((_ASSERTION_ID, None),),
    providers=("claude",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("live_token",),
    observed_activity=("claude_print_marker_returned",),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("claude_provider_token",),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=("provider_binary_receipt", "no_token_gate_canary", "real_print_canary_evidence"),
    required_cleanup=("real_print_process_exited",),
    implementation="server/zerg/qa/claude_turn_start_real_print.py",
    oracle_source="server/zerg/qa/provider_release_semantic_oracles.py",
    # See claude_launch_helm_real_print.py's REGISTRATION comment: this
    # scenario's oracle_source only declares assertion ids (assertions_for),
    # matching claude_real_print_qualification.py's own oracle_source for the
    # identical scenario -- not a per-observation judge function.
    oracle_entrypoint="assertions_for",
    executable_module="zerg.qa.claude_turn_start_real_print",
)

_ARTIFACT_KIND = "claude_turn_start_real_print_result"


def run_turn_start_scenario(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        provider_receipt = {"path": str(args.claude_bin), "sha256": sha256_file(args.claude_bin)}
        write_json(root / "provider-binary-receipt.json", provider_receipt)

        evidence_root = root / "semantic-evidence"
        with temporary_environment({real_print_qual.LIVE_ENABLE_ENV: "1"}):
            observation, assertions, credentials = real_print_qual._execute(args.claude_bin, evidence_root)

        assertions_map = {item.assertion_id: item.outcome == AssertionOutcome.PASS for item in assertions}
        marker_returned = assertions_map.get(_ASSERTION_ID, False)

        # _execute() is normally called by run_semantic_profile, which
        # redacts observed secrets from both the observation dict and the
        # evidence tree afterward. Calling _execute() directly here means
        # this producer owns that same redaction step.
        observation = redact_semantic_value(observation, credentials)
        scrub_semantic_tree(root, credentials)

        write_json(root / "semantic-observation.json", observation)
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": _ARTIFACT_KIND,
            "producer": REGISTRATION.to_dict(),
            "provider": "claude",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now_iso(),
            "status": "pass" if marker_returned else "fail",
            "observation": observation,
            "assertions": assertions_map,
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
            "evidence_class": "live_token",
            "generated_at": now_iso(),
            "status": "fail",
            "failure_code": "claude_turn_start_real_print_failed",
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
    result = run_turn_start_scenario(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
