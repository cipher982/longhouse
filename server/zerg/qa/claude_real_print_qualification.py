"""Exact-binary Claude CLI contract plus opt-in real-print qualification."""

from __future__ import annotations

import argparse
import os
import tempfile
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


def claude_real_print_oracle(
    *,
    no_token_verdict: str,
    live_enabled: bool,
    live_status: str | None,
) -> tuple[semantic.SemanticAssertion, ...]:
    """Pure: observation -> typed capability-proof assertion records.

    No I/O, no subprocess, no env reads. Extracted per Phase 2 step 1 (see
    opencode_server_qualification.opencode_server_contract_oracle for the
    pattern). `_execute` still decides *whether* to run the live canary
    (credential discovery is inherently impure); this function only judges
    what was observed.
    """
    no_token_outcome = _status_outcome(str(no_token_verdict).replace("green", "pass").replace("yellow", "warn").replace("red", "fail"))
    if live_enabled:
        live_outcome = AssertionOutcome.PASS if live_status == "pass" else AssertionOutcome.SEMANTIC_FAIL
        live_evidence_class = EvidenceClass.LIVE_TOKEN
    else:
        live_outcome = AssertionOutcome.BLOCKED
        live_evidence_class = EvidenceClass.LIVE_NO_TOKEN
    return (
        semantic.SemanticAssertion(ASSERTIONS[0], no_token_outcome, EvidenceClass.LIVE_NO_TOKEN),
        semantic.SemanticAssertion(ASSERTIONS[1], live_outcome, live_evidence_class),
    )


def _execute(binary: Path, evidence_root: Path):
    no_token_root = evidence_root / "no-token"
    no_token_home = no_token_root / "home"
    no_token_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    no_token_env: dict[str, str | None] = {
        "HOME": str(no_token_home),
        **dict.fromkeys(EXPLICIT_CREDENTIAL_ENV),
    }
    with semantic.temporary_environment(no_token_env):
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
        env: dict[str, str | None] = {"LONGHOUSE_CLAUDE_BIN": str(binary), **credentials}
        # Provider runtime state is disposable execution machinery, not
        # retained evidence. In particular, a profile-authenticated Claude
        # run can materialize credential-bearing files below HOME even when
        # the final JSON/log scrubber knows the configured token value.
        with tempfile.TemporaryDirectory(prefix="longhouse-claude-runtime-") as runtime_root:
            isolated_home = Path(runtime_root) / "home"
            isolated_home.mkdir(mode=0o700)
            env["HOME"] = str(isolated_home)
            with semantic.temporary_environment(env):
                live = module.run_claude_real_print_canary(argparse.Namespace(claude_print_timeout_secs=180), live_root)
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
    assertions = claude_real_print_oracle(
        no_token_verdict=str(no_token.get("verdict") or "red"),
        live_enabled=live_enabled,
        live_status=live.get("status") if live_enabled else None,
    )
    no_token_outcome, live_outcome = assertions[0].outcome, assertions[1].outcome
    overall = "pass"
    if AssertionOutcome.SEMANTIC_FAIL in {no_token_outcome, live_outcome}:
        overall = "fail"
    elif AssertionOutcome.BLOCKED in {no_token_outcome, live_outcome}:
        overall = "blocked"
    live_model_evidence = None
    if isinstance(live, dict):
        source_artifacts = [
            {
                "path": live[path_key],
                "sha256": live[digest_key],
                "kind": "provider_jsonl_stream" if path_key == "stdout_path" else "provider_stderr",
                **({"event_type": (live.get("result_event") or {}).get("type")} if path_key == "stdout_path" else {}),
                **({"event_sha256": (live.get("result_event") or {}).get("native_event_sha256")} if path_key == "stdout_path" else {}),
            }
            for path_key, digest_key in (("stdout_path", "stdout_sha256"), ("stderr_path", "stderr_sha256"))
            if isinstance(live.get(path_key), str) and isinstance(live.get(digest_key), str) and live.get(digest_key)
        ]
        result_event = live.get("result_event")
        if isinstance(result_event, dict) and live.get("model") and not result_event.get("model_source"):
            result_event = {**result_event, "model_source": "invocation"}
        live_model_evidence = {
            "source_canary": "real_print_canary",
            "operation_evidence": live.get("operation_evidence"),
            "model": live.get("model"),
            "result_event": result_event,
            "source_artifacts": source_artifacts,
        }
    return (
        {
            "status": overall,
            "no_token_canary": no_token,
            "real_print_canary": live,
            "live_model_evidence": live_model_evidence,
        },
        assertions,
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
