"""Strict exact-binary qualification profiles for provider conversation reset."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from zerg.qa import antigravity_release_identity
from zerg.qa import claude_release_identity
from zerg.qa import codex_release_identity
from zerg.qa import cursor_release_identity
from zerg.qa import opencode_release_identity
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.conversation_reset import evaluate_reset_observation
from zerg.qa.conversation_reset import load_reset_observation
from zerg.qa.conversation_reset import produce_live_reset_artifact
from zerg.qa.conversation_reset import reset_artifact_env
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

PROFILE_BY_PROVIDER = {
    provider: f"{provider}_conversation_reset_v1" for provider in ("codex", "claude", "opencode", "antigravity", "cursor")
}
ASSERTIONS = (
    "provider_conversation_reset_observed",
    "raw_history_conserved_across_reset",
    "longhouse_reset_binding_current",
)
_VERSION_LINES = {
    "codex": codex_release_identity._VERSION_LINE,  # noqa: SLF001
    "claude": claude_release_identity.VERSION_LINE,
    "opencode": opencode_release_identity.VERSION_LINE,
    "antigravity": antigravity_release_identity.VERSION_LINE,
    "cursor": cursor_release_identity.VERSION_LINE,
}
_PROVIDER_ASSERTIONS = frozenset(
    {
        "reset_command_accepted_quiescent",
        "provider_identity_transition_observed",
        "pre_reset_history_retained",
        "post_reset_turn_bound_to_active_identity",
        "pre_reset_messages_not_copied_to_new_history",
        "reset_kept_cli_invocation",
    }
)
_ARCHIVE_ASSERTIONS = frozenset(
    {
        "pre_reset_raw_preserved",
        "post_reset_raw_preserved",
        "reset_boundary_observable",
        "tail_order_conserved",
        "source_identity_preserved",
    }
)


def _axis_outcome(assertions: dict[str, bool], names: frozenset[str]) -> AssertionOutcome:
    return AssertionOutcome.PASS if all(assertions.get(name) is True for name in names) else AssertionOutcome.SEMANTIC_FAIL


def _longhouse_binding_current(observation: dict[str, Any]) -> bool:
    before = observation.get("before")
    after = observation.get("after")
    longhouse = observation.get("longhouse")
    if not isinstance(before, dict) or not isinstance(after, dict) or not isinstance(longhouse, dict):
        return False
    before_longhouse_id = str(before.get("longhouse_session_id") or "").strip()
    after_longhouse_id = str(after.get("longhouse_session_id") or "").strip()
    after_provider_id = str(after.get("provider_session_id") or "").strip()
    aliases = longhouse.get("provider_alias_ids")
    timeline = longhouse.get("timeline_session_ids")
    return bool(
        before_longhouse_id
        and before_longhouse_id == after_longhouse_id
        and after_provider_id
        and isinstance(aliases, list)
        and after_provider_id in aliases
        and longhouse.get("provider_alias_matches_after") is True
        and isinstance(timeline, list)
        and after_longhouse_id in timeline
    )


def _executor(provider: str, binary: Path, evidence_root: Path):
    artifact_value = str(os.environ.get(reset_artifact_env(provider)) or "").strip()
    if not artifact_value:
        produced = produce_live_reset_artifact(provider, binary, evidence_root / "producer")
        artifact_value = str(produced or "")
    if not artifact_value:
        observation = {
            "status": "blocked",
            "failure_code": "conversation_reset_artifact_missing",
            "required_env": reset_artifact_env(provider),
        }
        assertions = tuple(semantic.SemanticAssertion(name, AssertionOutcome.BLOCKED, EvidenceClass.LIVE_TOKEN) for name in ASSERTIONS)
        return observation, assertions, ()

    artifact_path = Path(artifact_value).expanduser().resolve(strict=True)
    observation = load_reset_observation(artifact_path)
    expected_identity = identity.sha256_file(binary)
    identity_matches = observation.get("provider_executable_identity") == expected_identity
    provider_matches = observation.get("provider") == provider
    evidence_matches = observation.get("evidence_class") == "live_token"
    evaluation = evaluate_reset_observation(observation)
    evaluated = dict(evaluation.get("assertions") or {})
    provider_outcome = _axis_outcome(evaluated, _PROVIDER_ASSERTIONS)
    archive_outcome = _axis_outcome(evaluated, _ARCHIVE_ASSERTIONS)
    binding_outcome = AssertionOutcome.PASS if _longhouse_binding_current(observation) else AssertionOutcome.SEMANTIC_FAIL
    if not (identity_matches and provider_matches and evidence_matches):
        provider_outcome = archive_outcome = binding_outcome = AssertionOutcome.SEMANTIC_FAIL
    outcomes = (provider_outcome, archive_outcome, binding_outcome)
    status = "pass" if set(outcomes) == {AssertionOutcome.PASS} else "fail"
    result = {
        "status": status,
        "failure_code": None if status == "pass" else "conversation_reset_qualification_failed",
        "artifact_path": str(artifact_path),
        "exact_binary_identity_matched": identity_matches,
        "provider_matched": provider_matches,
        "evidence_class_matched": evidence_matches,
        "evaluation": evaluation,
        "observation": observation,
    }
    assertions = tuple(
        semantic.SemanticAssertion(name, outcome, EvidenceClass.LIVE_TOKEN) for name, outcome in zip(ASSERTIONS, outcomes, strict=True)
    )
    return result, assertions, ()


def run(provider: str, request_path: Path, output_root: Path) -> dict[str, Any]:
    try:
        profile_name = PROFILE_BY_PROVIDER[provider]
        version_line = _VERSION_LINES[provider]
    except KeyError:
        raise identity.RequestError(f"unsupported conversation-reset provider: {provider}") from None
    profile = identity.IdentityProfile(
        provider=provider,
        profile=profile_name,
        scenario_id=f"{provider}_conversation_reset",
        version_line=version_line,
        oracle_source=Path(__file__).with_name("conversation_reset.py"),
    )
    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=profile,
        assertion_ids=ASSERTIONS,
        executor=lambda binary, evidence_root: _executor(provider, binary, evidence_root),
        oracle_source=Path(__file__).with_name("conversation_reset.py"),
    )
