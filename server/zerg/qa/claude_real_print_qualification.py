"""Exact-binary Claude CLI contract plus opt-in real-print qualification."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from zerg.qa import claude_release_identity
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_release_semantic_oracles as semantic_oracles
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_live_canary import run_provider_live_canary
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

PROFILE = "claude_real_print_v1"
SCENARIO_ID = "claude_real_print"
ASSERTIONS = semantic_oracles.assertions_for(SCENARIO_ID)
LIVE_ENABLE_ENV = "LONGHOUSE_CLAUDE_QUALIFICATION_LIVE"
EXPLICIT_CREDENTIAL_ENV = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ANTHROPIC_MODEL",
)
_PROFILE = identity.IdentityProfile(
    provider="claude",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=claude_release_identity.VERSION_LINE,
    oracle_source=Path(__file__),
)


def _status_outcome(status: str) -> AssertionOutcome:
    if status == "pass":
        return AssertionOutcome.PASS
    if status == "warn":
        return AssertionOutcome.BLOCKED
    return AssertionOutcome.SEMANTIC_FAIL


def _execute(binary: Path, evidence_root: Path):
    no_token_root = evidence_root / "no-token"
    no_token = run_provider_live_canary(
        {
            "provider": "claude",
            "provider_bin": str(binary),
            "artifact": no_token_root / "provider-live-canary.json",
            "evidence_root": no_token_root,
            "wait_ready_secs": 15.0,
            "json": False,
        }
    )
    no_token_outcome = _status_outcome(
        str(no_token.get("verdict") or "red").replace("green", "pass").replace("yellow", "warn").replace("red", "fail")
    )
    credentials = {key: value for key in EXPLICIT_CREDENTIAL_ENV if (value := str(os.environ.get(key) or "").strip())}
    explicit_authority = bool(
        credentials.get("CLAUDE_CONFIG_DIR")
        or credentials.get("CLAUDE_CODE_OAUTH_TOKEN")
        or credentials.get("ANTHROPIC_API_KEY")
        or credentials.get("CLAUDE_CODE_USE_BEDROCK", "").lower() in {"1", "true", "yes"}
    )
    live_enabled = no_token_outcome is AssertionOutcome.PASS and os.environ.get(LIVE_ENABLE_ENV) == "1" and explicit_authority
    live: dict[str, Any]
    if live_enabled:
        module = semantic.load_control_canary_module(Path(__file__).resolve().parents[3])
        live_root = evidence_root / "live"
        live_root.mkdir(parents=True, exist_ok=True)
        env = {"LONGHOUSE_CLAUDE_BIN": str(binary), **credentials}
        if "CLAUDE_CONFIG_DIR" not in credentials and not credentials.get("CLAUDE_CODE_USE_BEDROCK"):
            isolated_home = live_root / "home"
            isolated_home.mkdir(mode=0o700)
            env["HOME"] = str(isolated_home)
        with semantic.temporary_environment(env):
            live = module.run_claude_real_print_canary(argparse.Namespace(claude_print_timeout_secs=180), live_root)
        live_outcome = AssertionOutcome.PASS if live.get("status") == "pass" else AssertionOutcome.SEMANTIC_FAIL
        live_evidence_class = EvidenceClass.LIVE_TOKEN
    else:
        live = {
            "status": "blocked",
            "failure_code": (
                "explicit_claude_qualification_credentials_missing"
                if no_token_outcome is AssertionOutcome.PASS
                else "claude_no_token_contract_not_proven"
            ),
            "required_enable_env": LIVE_ENABLE_ENV,
            "accepted_credential_env": list(EXPLICIT_CREDENTIAL_ENV),
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
        tuple(credentials[key] for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY") if key in credentials),
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
