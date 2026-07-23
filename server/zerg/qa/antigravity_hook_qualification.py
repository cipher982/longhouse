"""Exact-binary Antigravity hook/inbox plus opt-in real-print qualification."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from zerg.qa import antigravity_release_identity
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_release_semantic_oracles as semantic_oracles
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_live_canary import run_provider_live_canary
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

PROFILE = "antigravity_hook_inbox_v1"
SCENARIO_ID = "antigravity_hook_inbox"
ASSERTIONS = semantic_oracles.assertions_for(SCENARIO_ID)
LIVE_ENABLE_ENV = "LONGHOUSE_ANTIGRAVITY_QUALIFICATION_LIVE"
QUALIFICATION_HOME_ENV = "ANTIGRAVITY_QUALIFICATION_HOME"
_PROFILE = identity.IdentityProfile(
    provider="antigravity",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=antigravity_release_identity.VERSION_LINE,
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
    no_token_root = evidence_root / "no-token"
    no_token_home = no_token_root / "home"
    no_token_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with semantic.temporary_environment({"HOME": str(no_token_home)}):
        no_token = run_provider_live_canary(
            {
                "provider": "antigravity",
                "provider_bin": str(binary),
                "artifact": no_token_root / "provider-live-canary.json",
                "evidence_root": no_token_root,
                "wait_ready_secs": 15.0,
                "strict_provider_binary": True,
                "json": False,
            }
        )
    no_token_outcome = _group_outcome(
        dict(no_token.get("canaries") or {}),
        (
            "binary_identity",
            "command_shape",
            "plugin_contract",
            "global_hooks_contract",
            "hook_inbox_claim_contract",
        ),
    )
    raw_home = str(os.environ.get(QUALIFICATION_HOME_ENV) or "").strip()
    live_requested = os.environ.get(LIVE_ENABLE_ENV) == "1" or bool(raw_home)
    # This qualification runner executes on a Machine Agent host. Antigravity
    # has no supported authenticated profile/data-root override, so any real
    # ``agy --print`` here can create a Shadow transcript that is
    # indistinguishable from a user's native session. Do not attempt to infer
    # isolation from HOME or a path convention. A real proof belongs on a
    # separately provisioned unwatched worker with its own producer boundary.
    # Until that worker exists, block before invoking the print canary.
    live: dict[str, Any] = {
        "status": "blocked",
        "failure_code": (
            "antigravity_unwatched_producer_boundary_unavailable"
            if no_token_outcome is AssertionOutcome.PASS and live_requested
            else "explicit_antigravity_qualification_authority_missing"
            if no_token_outcome is AssertionOutcome.PASS
            else "antigravity_no_token_contract_not_proven"
        ),
        "enable_env": LIVE_ENABLE_ENV,
        "qualification_home_env": QUALIFICATION_HOME_ENV,
        "producer_boundary": "unwatched_worker_required",
    }
    live_outcome = AssertionOutcome.BLOCKED
    live_evidence_class = EvidenceClass.LIVE_NO_TOKEN
    overall = "pass"
    if AssertionOutcome.SEMANTIC_FAIL in {no_token_outcome, live_outcome}:
        overall = "fail"
    elif AssertionOutcome.BLOCKED in {no_token_outcome, live_outcome}:
        overall = "blocked"
    return (
        {"status": overall, "no_token_canary": no_token, "real_print_canary": live},
        (
            semantic.SemanticAssertion(ASSERTIONS[0], no_token_outcome, EvidenceClass.LIVE_NO_TOKEN),
            semantic.SemanticAssertion(ASSERTIONS[1], live_outcome, live_evidence_class),
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
