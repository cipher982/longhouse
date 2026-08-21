"""Pure eligibility and CAS contracts for legacy sticky-hidden Codex Helm rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CodexLaunchVisibilityRepairFacts:
    session_id: str
    provider: str | None
    mode: str | None
    execution_home: str | None
    control_ownership: str | None
    fresh_exact_terminal_attached: bool
    fresh_exact_active_run: bool
    launch_actor: str | None
    launch_surface: str | None
    origin_kind: str | None
    is_sidechain: bool
    environment: str | None
    hidden_from_default_timeline: bool
    primary_thread_hidden_from_default_timeline: bool
    user_hidden_from_timeline: bool


@dataclass(frozen=True, slots=True)
class CodexLaunchVisibilityRepairPlan:
    session_id: str
    compare_and_set: dict[str, Any]
    updates: dict[str, Any]


def codex_launch_visibility_repair_fingerprint(
    plan: CodexLaunchVisibilityRepairPlan,
) -> str:
    """Bind an apply request to the complete facts returned by dry-run."""

    payload = {
        "session_id": plan.session_id,
        "compare_and_set": plan.compare_and_set,
        "updates": plan.updates,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def codex_launch_visibility_repair_refusals(
    facts: CodexLaunchVisibilityRepairFacts,
) -> tuple[str, ...]:
    """Name every failed eligibility fact without weakening the gate."""

    refusals: list[str] = []
    checks = (
        (facts.provider == "codex", "provider_not_codex"),
        (facts.mode == "helm", "mode_not_helm"),
        (facts.execution_home == "managed_local", "execution_home_not_managed_local"),
        (facts.control_ownership == "owned", "control_not_owned"),
        (
            facts.fresh_exact_terminal_attached or facts.fresh_exact_active_run,
            "fresh_exact_open_evidence_missing",
        ),
        (facts.launch_actor is None, "launch_actor_already_set"),
        (facts.launch_surface is None, "launch_surface_already_set"),
        (facts.origin_kind is None, "hidden_origin_present"),
        (not facts.is_sidechain, "sidechain"),
        (facts.environment is not None, "environment_missing"),
        (facts.environment not in {"test", "e2e"}, "test_environment"),
        (facts.hidden_from_default_timeline, "not_policy_hidden"),
        (not facts.user_hidden_from_timeline, "user_hidden"),
    )
    for accepted, reason in checks:
        if not accepted:
            refusals.append(reason)
    return tuple(refusals)


def plan_codex_launch_visibility_repair(
    facts: CodexLaunchVisibilityRepairFacts,
) -> CodexLaunchVisibilityRepairPlan | None:
    """Return the complete CAS contract for one unambiguous legacy row."""

    if codex_launch_visibility_repair_refusals(facts):
        return None

    # Every eligibility field is repeated in the compare-and-set contract.
    # A later operator runner must re-read/re-project and refuse the update if
    # any value changed between its dry-run report and apply batch.
    expected = {
        "provider": facts.provider,
        "mode": facts.mode,
        "execution_home": facts.execution_home,
        "control_ownership": facts.control_ownership,
        "fresh_exact_terminal_attached": facts.fresh_exact_terminal_attached,
        "fresh_exact_active_run": facts.fresh_exact_active_run,
        "launch_actor": None,
        "launch_surface": None,
        "origin_kind": None,
        "is_sidechain": False,
        "environment": facts.environment,
        "hidden_from_default_timeline": True,
        "primary_thread_hidden_from_default_timeline": facts.primary_thread_hidden_from_default_timeline,
        "user_hidden_from_timeline": False,
    }
    return CodexLaunchVisibilityRepairPlan(
        session_id=facts.session_id,
        compare_and_set=expected,
        updates={
            "launch_actor": "human_shell",
            "launch_surface": "terminal",
            "hidden_from_default_timeline": False,
        },
    )


__all__ = [
    "CodexLaunchVisibilityRepairFacts",
    "CodexLaunchVisibilityRepairPlan",
    "codex_launch_visibility_repair_fingerprint",
    "codex_launch_visibility_repair_refusals",
    "plan_codex_launch_visibility_repair",
]
