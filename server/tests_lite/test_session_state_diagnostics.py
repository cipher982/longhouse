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
    assert raced.activity is None and raced.control is None


def test_diagnostics_route_is_explicitly_non_cutover(monkeypatch):
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
        lambda _facts, observed_at: SimpleNamespace(session_state=_legacy()),
    )
    monkeypatch.setattr(
        diagnostics_router,
        "project_shadow_session_state_facts",
        lambda **_kwargs: shadow,
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
    assert payload["served_path"] == "legacy_session_state"
    assert payload["authorization_path"] == "legacy_capabilities"
    assert payload["cutover_active"] is False
