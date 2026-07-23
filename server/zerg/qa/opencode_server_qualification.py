"""Exact-binary OpenCode serve/session/restart qualification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zerg.qa import opencode_release_identity
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_release_semantic_oracles as semantic_oracles
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_live_canary import run_provider_live_canary
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

PROFILE = "opencode_server_contract_v1"
SCENARIO_ID = "opencode_server_contract"
ASSERTIONS = semantic_oracles.assertions_for(SCENARIO_ID)
_PROFILE = identity.IdentityProfile(
    provider="opencode",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=opencode_release_identity.VERSION_LINE,
    oracle_source=Path(__file__),
)


def _group_outcome(canaries: dict[str, Any], required: tuple[str, ...]) -> AssertionOutcome:
    statuses = [str((canaries.get(name) or {}).get("status") or "missing") for name in required]
    if all(status == "pass" for status in statuses):
        return AssertionOutcome.PASS
    if "fail" in statuses:
        return AssertionOutcome.SEMANTIC_FAIL
    return AssertionOutcome.INFRASTRUCTURE_ERROR


def _execute(binary: Path, evidence_root: Path):
    artifact = run_provider_live_canary(
        {
            "provider": "opencode",
            "provider_bin": str(binary),
            "artifact": evidence_root / "provider-live-canary.json",
            "evidence_root": evidence_root,
            "wait_ready_secs": 15.0,
            "json": False,
        }
    )
    canaries = dict(artifact.get("canaries") or {})
    serve_outcome = _group_outcome(
        canaries,
        (
            "binary_identity",
            "attach_command_shape",
            "server_startup",
            "schema_probe",
            "session_create",
            "session_get",
            "prompt_async_no_reply_delivery",
            "session_abort",
        ),
    )
    restart_outcome = _group_outcome(
        canaries,
        ("binary_identity", "process_restart_reattach_contract"),
    )
    overall = "pass" if {serve_outcome, restart_outcome} == {AssertionOutcome.PASS} else "fail"
    return (
        {"status": overall, "provider_live_canary": artifact},
        (
            semantic.SemanticAssertion(ASSERTIONS[0], serve_outcome, EvidenceClass.LIVE_NO_TOKEN),
            semantic.SemanticAssertion(ASSERTIONS[1], restart_outcome, EvidenceClass.LIVE_NO_TOKEN),
        ),
        (),
    )


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=ASSERTIONS,
        executor=_execute,
        oracle_source=Path(semantic_oracles.__file__),
    )
