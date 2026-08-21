"""Fail-closed semantic postconditions for live Helm launch qualification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ASSERTION_ID = "helm_launch_visibility_preserved"
SCENARIO_ID = "codex_helm_launch_visibility"


def _human_launch_ok(observation: object, *, resumed: bool) -> bool:
    if not isinstance(observation, Mapping):
        return False
    registration = observation.get("registration")
    canonical = observation.get("canonical")
    if not isinstance(registration, Mapping) or not isinstance(canonical, Mapping):
        return False
    return (
        registration.get("provider") == "codex"
        and registration.get("launch_actor") == "human_shell"
        and registration.get("launch_surface") == "terminal"
        and registration.get("session_id") == canonical.get("session_id")
        and canonical.get("mode") == "helm"
        and canonical.get("working_set") == "open"
        and canonical.get("control_head_current") is True
        and canonical.get("control_run_id") == registration.get("run_id")
        and canonical.get("default_timeline_visible") is True
        and canonical.get("observed_within_seconds") is not None
        and float(canonical["observed_within_seconds"]) <= 45.0
        and bool(registration.get("resume_attempt_id")) is resumed
    )


def _automation_launch_ok(observation: object) -> bool:
    if not isinstance(observation, Mapping):
        return False
    registration = observation.get("registration")
    canonical = observation.get("canonical")
    if not isinstance(registration, Mapping) or not isinstance(canonical, Mapping):
        return False
    return (
        registration.get("provider") == "codex"
        and registration.get("launch_actor") == "automation"
        and registration.get("launch_surface") == "test"
        and canonical.get("default_timeline_visible") is False
    )


def _cleanup_ok(receipt: object) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    axes = receipt.get("axes")
    return (
        receipt.get("status") == "pass"
        and isinstance(axes, Mapping)
        and axes.get("default_timeline_absent") is True
        and axes.get("open_absent") is True
        and axes.get("title_debt_absent") is True
        and axes.get("workspace_suggestion_absent") is True
        and axes.get("direct_retrieval_succeeds") is True
        and axes.get("owned_processes_dead") is True
    )


def helm_launch_assertions(observation: Mapping[str, Any]) -> dict[str, bool]:
    """Require the complete launch transaction; missing provenance is failure."""

    cleanups = observation.get("cleanup")
    cleanup_ok = isinstance(cleanups, list) and len(cleanups) == 2 and all(_cleanup_ok(item) for item in cleanups)
    passed = (
        _human_launch_ok(observation.get("fresh"), resumed=False)
        and _human_launch_ok(observation.get("resumed"), resumed=True)
        and _automation_launch_ok(observation.get("automation"))
        and observation.get("same_session_resumed") is True
        and observation.get("new_run_on_resume") is True
        and observation.get("provenance_free_observation_rejected") is True
        and cleanup_ok
    )
    return {ASSERTION_ID: passed}


__all__ = ["ASSERTION_ID", "SCENARIO_ID", "helm_launch_assertions"]
