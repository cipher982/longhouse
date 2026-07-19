from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import zerg.routers.agents_state_diagnostics as diagnostics_router
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.session_state_contract import SessionStateFacts
from zerg.services.session_state_diagnostics import compare_session_state_axes
from zerg.services.session_state_diagnostics import compare_session_state_projections
from zerg.services.session_state_facts_projector import ShadowSessionStateProjection

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _action(state: str = "available", reason: str | None = None) -> dict[str, str | None]:
    return {"state": state, "reason": reason}


def _control() -> dict[str, object]:
    return {
        "ownership": "owned",
        "connection": "connected",
        "connection_id": "connection-1",
        "lease_generation": "lease-1",
        "control_plane": None,
        "observed_at": NOW,
        "valid_until": NOW + timedelta(minutes=1),
        "actions": {
            "send_input": _action(),
            "interrupt": _action(),
            "terminate": _action("unavailable", "not_granted"),
            "reattach": _action("unavailable", "unsupported"),
            "resume": _action("unavailable", "unsupported"),
        },
    }


def _legacy() -> SessionStateFacts:
    return SessionStateFacts.model_validate(
        {
            "mode": "helm",
            "disposition": {"state": "open"},
            "activity": {
                "state": "executing",
                "raw_kind": "running",
                "tool": "Shell",
                "source": "provider_runtime",
                "observed_at": NOW,
                "valid_until": NOW + timedelta(minutes=1),
            },
            "control": _control(),
            "transcript": {"convergence": "unknown"},
            "host": {"state": "unknown"},
            "presentation": {},
        }
    )


def _shadow(*, activity_state: str = "executing", commit_seq: int = 12) -> ShadowSessionStateProjection:
    return ShadowSessionStateProjection.model_validate(
        {
            "commit_seq": commit_seq,
            "mode": "helm",
            "disposition": {"state": "open"},
            "activity": {
                "state": activity_state,
                "raw_kind": "running",
                "tool": "Shell",
                "source": "provider_runtime",
                "observed_at": NOW,
                "valid_until": NOW + timedelta(minutes=1),
            },
            "control": _control(),
        }
    )


def test_comparison_is_scoped_to_projected_axes():
    comparison = compare_session_state_axes(
        legacy=_legacy(),
        shadow=_shadow(),
        legacy_commit_seq=12,
        shadow_commit_seq=12,
    )

    assert comparison.status == "matched"
    assert comparison.same_commit is True
    assert comparison.mode is not None and comparison.mode.matches is True
    assert comparison.disposition is not None and comparison.disposition.matches is True
    assert comparison.launch is not None and comparison.launch.matches is True
    assert comparison.run is not None and comparison.run.matches is True
    assert comparison.activity is not None and comparison.activity.matches is True
    assert comparison.control is not None and comparison.control.matches is True


def test_comparison_reports_axis_drift_and_rejects_cross_commit_claims():
    drift = compare_session_state_axes(
        legacy=_legacy(),
        shadow=_shadow(activity_state="quiescent"),
        legacy_commit_seq=12,
        shadow_commit_seq=12,
    )
    assert drift.status == "different"
    assert drift.activity is not None and drift.activity.matches is False
    assert drift.control is not None and drift.control.matches is True

    raced = compare_session_state_axes(
        legacy=_legacy(),
        shadow=_shadow(commit_seq=13),
        legacy_commit_seq=12,
        shadow_commit_seq=13,
    )
    assert raced.status == "not_comparable"
    assert raced.same_commit is False
    assert raced.mode is None and raced.disposition is None
    assert raced.activity is None and raced.control is None

    shadow = _shadow()
    assert shadow.control is not None
    changed_actions = shadow.control.actions.model_copy(
        update={"start_turn": shadow.control.actions.start_turn.model_copy(update={"state": "available", "reason": None})}
    )
    action_drift = compare_session_state_axes(
        legacy=_legacy(),
        shadow=shadow.model_copy(update={"control": shadow.control.model_copy(update={"actions": changed_actions})}),
        legacy_commit_seq=12,
        shadow_commit_seq=12,
    )
    assert action_drift.status == "different"
    assert action_drift.control is not None and action_drift.control.matches is False


def test_projection_parity_detects_version_and_presentation_key_divergence():
    state = _legacy().model_copy(update={"commit_seq": 12})
    matched = compare_session_state_projections(
        session_state=state,
        machine_payload={
            "commit_seq": "12",
            "state_contract_version": 1,
            "presentation_policy_version": 1,
            "presentation": {"primary": None, "access": None},
        },
    )
    assert matched.status == "matched"

    diverged = compare_session_state_projections(
        session_state=state,
        machine_payload={
            "commit_seq": "12",
            "state_contract_version": 2,
            "presentation_policy_version": 1,
            "presentation": {"primary": {"key": "idle"}, "access": None},
        },
    )
    assert diverged.status == "diverged"
    assert diverged.mismatched_fields == ("state_contract_version", "primary_key")

    raced = compare_session_state_projections(
        session_state=state,
        machine_payload={
            "commit_seq": "13",
            "state_contract_version": 1,
            "presentation_policy_version": 1,
            "presentation": {"primary": None, "access": None},
        },
    )
    assert raced.status == "not_comparable"


def test_control_identity_comparison_distinguishes_bound_unbound_and_mismatch():
    shadow = _shadow().model_copy(update={"control_run_id": "run-1"})
    bound = {
        "connections": [
            {
                "id": 7,
                "run_id": "run-1",
                "adapter_connection_id": "connection-1",
                "lease_generation": "lease-1",
                "acquired_at": NOW.isoformat(),
            }
        ]
    }
    matched = compare_session_state_axes(
        legacy=_legacy(),
        shadow=shadow,
        legacy_commit_seq=12,
        shadow_commit_seq=12,
        catalog_facts=bound,
    )
    assert matched.control_identity is not None
    assert matched.control_identity.status == "bound_matched"
    assert matched.control_identity.legacy_grant == {
        "catalog_connection_id": 7,
        "connection_id": "connection-1",
        "lease_generation": "lease-1",
        "identity_source": "adapter_bound",
    }
    assert matched.control_identity.catalog_bound_count == 1

    unbound = compare_session_state_axes(
        legacy=_legacy(),
        shadow=shadow,
        legacy_commit_seq=12,
        shadow_commit_seq=12,
        catalog_facts={"connections": [{"id": 7, "run_id": "run-1", "acquired_at": NOW.isoformat()}]},
    )
    assert unbound.control_identity is not None and unbound.control_identity.status == "unbound"
    assert unbound.control_identity.catalog is None
    assert unbound.control_identity.legacy_grant is None
    assert unbound.control_identity.catalog_bound_count == 0

    unknown = compare_session_state_axes(
        legacy=_legacy(),
        shadow=shadow,
        legacy_commit_seq=12,
        shadow_commit_seq=12,
        catalog_facts={
            "connections": [
                {
                    "id": 7,
                    "run_id": "run-1",
                    "adapter_connection_id": "different-connection",
                    "lease_generation": "different-lease",
                    "acquired_at": NOW.isoformat(),
                }
            ]
        },
    )
    assert unknown.control_identity is not None
    assert unknown.control_identity.status == "binding_unknown"
    assert unknown.control_identity.catalog is None
    assert unknown.control_identity.catalog_bound_count == 1

    diverged = compare_session_state_axes(
        legacy=_legacy(),
        shadow=shadow,
        legacy_commit_seq=12,
        shadow_commit_seq=12,
        catalog_facts={
            "connections": [
                {
                    "id": 7,
                    "run_id": "different-run",
                    "adapter_connection_id": "connection-1",
                    "lease_generation": "different-lease",
                    "acquired_at": NOW.isoformat(),
                }
            ]
        },
    )
    assert diverged.control_identity is not None
    assert diverged.control_identity.status == "identity_diverged"


def test_control_identity_comparison_reports_legacy_only_without_control_evidence():
    comparison = compare_session_state_axes(
        legacy=_legacy(),
        shadow=_shadow().model_copy(update={"control": None, "control_run_id": None}),
        legacy_commit_seq=12,
        shadow_commit_seq=12,
        catalog_facts={"connections": []},
    )

    assert comparison.control_identity is not None
    assert comparison.control_identity.status == "legacy_only"
    assert comparison.control_identity.evidence is None
    assert comparison.control_identity.catalog is None
    assert comparison.control_identity.legacy_grant is None


def test_diagnostics_route_reports_detail_cutover_without_claiming_authorization_cutover(monkeypatch):
    session_id = "44444444-4444-4444-8444-444444444444"
    shadow = _shadow()
    monkeypatch.setattr(diagnostics_router.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        diagnostics_router,
        "shadow_session_state_snapshot",
        lambda requested_session_id, owner_id: {
            "found": True,
            "commit_seq": "12",
            "observed_at": NOW.isoformat(),
            "provider": "codex",
            "head_count": 2,
            "legacy_facts": {"catalog": {"session_id": requested_session_id}},
            "heads": [],
        },
    )
    monkeypatch.setattr(
        diagnostics_router,
        "project_catalog_session_facts",
        lambda _facts, observed_at, **_kwargs: SimpleNamespace(session_state=_legacy()),
    )
    monkeypatch.setattr(
        diagnostics_router,
        "project_shadow_session_state_facts",
        lambda **_kwargs: shadow,
    )
    monkeypatch.setattr(
        diagnostics_router,
        "project_machine_session_delta",
        lambda _session, *, commit_seq, canonical: {
            "commit_seq": str(commit_seq),
            "state_contract_version": 1,
            "presentation_policy_version": 1,
            "presentation": {"primary": None, "access": None},
        },
    )
    app = FastAPI()
    app.include_router(diagnostics_router.router, prefix="/api")
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=7)
    app.dependency_overrides[require_single_tenant] = lambda: None

    response = TestClient(app).get(f"/api/agents/sessions/{session_id}/state-diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["catalog_commit_seq"] == 12
    assert payload["comparison"]["status"] == "matched"
    assert payload["comparison"]["control_identity"]["status"] == "unbound"
    assert payload["served_path"] == "legacy_session_state"
    assert payload["authorization_path"] == "legacy_capabilities"
    assert payload["canonical_authorization_providers"] == []
    assert payload["cutover_active"] is False
    assert payload["authorization_cutover_active"] is False
    assert payload["explain"]["commit_seq"] == 12
    assert payload["explain"]["state_contract_version"] == 1
    assert payload["explain"]["presentation_policy_version"] == 1
    assert payload["explain"]["presentation_keys"] == {
        "primary": None,
        "access": None,
        "transcript": None,
    }
    assert payload["explain"]["actions"]["send_input"] == {"state": "available", "reason": None}

    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_DETAIL_SERVE", "canonical")
    canonical_response = TestClient(app).get(f"/api/agents/sessions/{session_id}/state-diagnostics")

    assert canonical_response.status_code == 200
    canonical_payload = canonical_response.json()
    assert canonical_payload["served_path"] == "canonical_session_detail"
    assert canonical_payload["authorization_path"] == "legacy_capabilities"
    assert canonical_payload["cutover_active"] is True
    assert canonical_payload["authorization_cutover_active"] is False
    assert canonical_payload["explain"]["projection_parity"]["status"] == "matched"

    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_COMMAND_AUTH", "canonical")
    command_response = TestClient(app).get(f"/api/agents/sessions/{session_id}/state-diagnostics")

    assert command_response.status_code == 200
    command_payload = command_response.json()
    assert command_payload["served_path"] == "canonical_session_detail"
    assert command_payload["authorization_path"] == "provider_scoped_canonical_control"
    assert command_payload["canonical_authorization_providers"] == ["claude", "codex", "cursor", "opencode"]
    assert command_payload["authorization_cutover_active"] is True


def test_reducer_health_route_reports_failures_without_claiming_cutover(monkeypatch):
    monkeypatch.setattr(diagnostics_router.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        diagnostics_router,
        "shadow_session_state_health",
        lambda owner_id: {
            "found": True,
            "commit_seq": "21",
            "observed_at": NOW.isoformat(),
            "ingest_enabled": True,
            "parity_enabled": True,
            "storage": {
                "head_counts": {"activity": 4, "control": 2},
                "head_capacity_per_family": 2_048,
                "receipt_count": 8,
                "conflict_count": 1,
                "parity_delta_count": 2,
            },
            "recent_batches": {
                "sample_size": 3,
                "sample_limit": 100,
                "window_seconds": 900,
                "truncated": False,
                "newest_received_at": NOW.isoformat(),
                "oldest_received_at": (NOW - timedelta(seconds=2)).isoformat(),
                "malformed_results": 0,
                "reducer_status_counts": {"applied": 2, "failed": 1},
                "parity_status_counts": {"compared": 3},
                "changed_heads": 4,
                "duplicates": 2,
                "stale": 1,
                "conflicts": 1,
                "parity_deltas": 2,
                "parity_missing_heads": 0,
            },
        },
    )
    app = FastAPI()
    app.include_router(diagnostics_router.health_router, prefix="/api")
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=7)
    app.dependency_overrides[require_single_tenant] = lambda: None

    response = TestClient(app).get("/api/agents/session-state/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["catalog_commit_seq"] == 21
    assert payload["projected_families"] == ["mode", "disposition", "launch", "run", "activity", "control"]
    assert "transcript" in payload["unsupported_families"]
    assert payload["cutover_active"] is False
    assert payload["contract"]["state_contract_version"] == 1
    assert payload["contract"]["presentation_policy_version"] == 1
    assert payload["contract"]["presentation_keys"]["primary"][-1] == "activity_unknown"
    assert len(payload["contract"]["fingerprint"]) == 64


def test_reducer_health_distinguishes_not_reducing_from_not_comparable(monkeypatch):
    snapshot = {
        "found": True,
        "commit_seq": "22",
        "observed_at": NOW.isoformat(),
        "ingest_enabled": True,
        "parity_enabled": True,
        "storage": {
            "head_counts": {},
            "head_capacity_per_family": 2_048,
            "receipt_count": 0,
            "conflict_count": 0,
            "parity_delta_count": 0,
        },
        "recent_batches": {
            "sample_size": 1,
            "sample_limit": 100,
            "window_seconds": 900,
            "truncated": False,
            "newest_received_at": NOW.isoformat(),
            "oldest_received_at": NOW.isoformat(),
            "malformed_results": 0,
            "reducer_status_counts": {"no_evidence": 1},
            "parity_status_counts": {"no_evidence": 1},
            "changed_heads": 0,
            "duplicates": 0,
            "stale": 0,
            "conflicts": 0,
            "parity_deltas": 0,
            "parity_missing_heads": 0,
        },
    }
    monkeypatch.setattr(diagnostics_router.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(diagnostics_router, "shadow_session_state_health", lambda owner_id: snapshot)
    app = FastAPI()
    app.include_router(diagnostics_router.health_router, prefix="/api")
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=7)
    app.dependency_overrides[require_single_tenant] = lambda: None
    client = TestClient(app)

    assert client.get("/api/agents/session-state/health").json()["status"] == "not_reducing"

    snapshot["recent_batches"]["reducer_status_counts"] = {"applied": 1}
    snapshot["recent_batches"]["parity_status_counts"] = {"legacy_unavailable": 1}
    assert client.get("/api/agents/session-state/health").json()["status"] == "not_comparable"

    snapshot["parity_enabled"] = False
    snapshot["recent_batches"]["parity_status_counts"] = {"disabled": 1}
    assert client.get("/api/agents/session-state/health").json()["status"] == "not_comparable"
