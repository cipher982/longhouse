#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa" / "session-state-deep-health.py"
SPEC = importlib.util.spec_from_file_location("session_state_deep_health", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _health() -> dict:
    return {
        "served_path": "canonical_session_detail",
        "authorization_path": "provider_scoped_canonical_control",
        "canonical_authorization_providers": ["codex"],
        "contract": {
            "state_contract_version": 1,
            "presentation_policy_version": 1,
            "presentation_keys": {"primary": ["idle"], "access": ["live_control"]},
            "fingerprint": "a" * 64,
        },
    }


def _diagnostic(*, projection_status: str = "matched", provider: str = "codex") -> dict:
    return {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "provider": provider,
        "comparison": {"status": "matched"},
        "explain": {
            "commit_seq": 12,
            "state_contract_version": 1,
            "presentation_policy_version": 1,
            "presentation_keys": {"primary": "idle", "access": "live_control"},
            "fact_sources": {"activity": {"source": "provider_runtime"}},
            "actions": {"send_input": {"state": "available", "reason": None}},
            "projection_parity": {"status": projection_status},
        },
    }


def test_assess_passes_only_with_canonical_same_commit_surface_proof():
    artifact, errors = MODULE.assess(
        reducer_health=_health(),
        diagnostics=[_diagnostic()],
        build={"git_sha": "abc"},
        required_providers={"codex"},
        require_canonical=True,
        live_surface_parity={"status": "matched_same_commit"},
        allow_cross_commit_equivalence=False,
        allow_targeted_proof_required=False,
    )
    assert errors == []
    assert artifact["status"] == "pass"
    assert artifact["sessions"][0]["commit_seq"] == 12
    assert artifact["sessions"][0]["actions"]["send_input"]["state"] == "available"


def test_assess_fails_closed_on_surface_version_provider_or_cutover_drift():
    health = _health()
    health["served_path"] = "legacy_session_state"
    diagnostic = _diagnostic(projection_status="diverged")
    diagnostic["explain"]["state_contract_version"] = 2
    diagnostic["comparison"] = {"status": "different"}
    artifact, errors = MODULE.assess(
        reducer_health=health,
        diagnostics=[diagnostic],
        build=None,
        required_providers={"codex", "claude"},
        require_canonical=True,
        live_surface_parity={"status": "matched_equivalent_different_commit"},
        allow_cross_commit_equivalence=False,
        allow_targeted_proof_required=False,
    )
    assert artifact["status"] == "fail"
    assert "canonical detail serving is not active" in errors
    assert any("compact projection" in error for error in errors)
    assert any("version diverged" in error for error in errors)
    assert any("deletion-blocking deltas" in error for error in errors)
    assert any("claude" in error for error in errors)
    assert any("API/machine-stream" in error for error in errors)


def test_live_surface_comparison_distinguishes_same_commit_race_and_drift():
    state = {
        "state_contract_version": 1,
        "presentation_policy_version": 1,
        "mode": "helm",
        "presentation": {"primary": {"key": "idle"}, "access": {"key": "live_control"}},
        "activity": {"state": "quiescent"},
        "control": {
            "ownership": "owned",
            "connection": "connected",
            "actions": {"terminate": {"state": "available"}, "reattach": {"state": "unavailable"}},
        },
        "run": {"id": "run-1", "lifecycle": "running"},
        "pending_interaction": None,
    }
    detail = {"session_state": {**state, "commit_seq": 12}}
    delta = {**state, "session_id": "session-1", "commit_seq": "12"}
    assert MODULE.compare_live_surfaces(detail=detail, machine_delta=delta)["status"] == "matched_same_commit"
    delta["commit_seq"] = "13"
    assert MODULE.compare_live_surfaces(detail=detail, machine_delta=delta)["status"] == "matched_equivalent_different_commit"
    delta["presentation"] = {"primary": {"key": "thinking"}, "access": {"key": "live_control"}}
    drift = MODULE.compare_live_surfaces(detail=detail, machine_delta=delta)
    assert drift["status"] == "diverged"
    assert drift["mismatched_fields"] == ["primary_key"]


def test_assess_rejects_zero_session_sample():
    artifact, errors = MODULE.assess(
        reducer_health=_health(),
        diagnostics=[],
        build=None,
        required_providers=set(),
        require_canonical=True,
        live_surface_parity={"status": "matched_same_commit"},
        allow_cross_commit_equivalence=False,
        allow_targeted_proof_required=False,
    )
    assert artifact["status"] == "fail"
    assert errors == ["no sessions were sampled; deep health cannot pass vacuously"]


def test_assess_requires_explicit_opt_in_for_targeted_proof_sessions():
    diagnostic = _diagnostic()
    diagnostic["comparison"] = {"status": "different", "gate_status": "targeted_proof_required"}
    blocked, errors = MODULE.assess(
        reducer_health=_health(),
        diagnostics=[diagnostic],
        build={"git_sha": "abc"},
        required_providers={"codex"},
        require_canonical=True,
        live_surface_parity={"status": "matched_same_commit"},
        allow_cross_commit_equivalence=False,
        allow_targeted_proof_required=False,
    )
    assert blocked["status"] == "fail"
    assert errors == ["11111111-1111-4111-8111-111111111111: reducer comparison requires targeted proof"]

    allowed, errors = MODULE.assess(
        reducer_health=_health(),
        diagnostics=[diagnostic],
        build={"git_sha": "abc"},
        required_providers={"codex"},
        require_canonical=True,
        live_surface_parity={"status": "matched_same_commit"},
        allow_cross_commit_equivalence=False,
        allow_targeted_proof_required=True,
    )
    assert errors == []
    assert allowed["status"] == "pass"
    assert allowed["targeted_proof_sessions"] == ["11111111-1111-4111-8111-111111111111"]


if __name__ == "__main__":
    test_assess_passes_only_with_canonical_same_commit_surface_proof()
    test_assess_fails_closed_on_surface_version_provider_or_cutover_drift()
    test_live_surface_comparison_distinguishes_same_commit_race_and_drift()
    test_assess_rejects_zero_session_sample()
    test_assess_requires_explicit_opt_in_for_targeted_proof_sessions()
    print("session-state deep-health tests OK")
