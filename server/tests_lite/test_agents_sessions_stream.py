from __future__ import annotations

import asyncio
import json
import os
from builtins import anext
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")

import zerg.routers.agents_sessions as agents_sessions
import zerg.services.live_catalog_timeline as live_catalog_timeline
from zerg.services.live_catalog_timeline import project_machine_session_delta
from zerg.services.session_state_contract import SessionStateFacts


class _Request:
    async def is_disconnected(self) -> bool:
        return True


def test_machine_session_stream_uses_commit_driven_catalog_projection(monkeypatch):
    captured = {}

    async def fake_stream(request, *, params, skip_initial_replay, owner_id):
        captured["request"] = request
        captured["params"] = params
        captured["skip_initial_replay"] = skip_initial_replay
        captured["owner_id"] = owner_id
        yield {"event": "connected", "data": "{}"}

    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "stream_live_catalog_machine_sessions", fake_stream)
    request = _Request()

    response = asyncio.run(
        agents_sessions.stream_agent_sessions(
            request,
            device_id="cinder",
            days_back=7,
            limit=8,
            skip_initial_replay=False,
            _auth=SimpleNamespace(owner_id=1),
            _single=None,
        )
    )

    assert response.headers["x-limit-cap"] == "100"
    event = asyncio.run(anext(response.body_iterator))
    assert event["event"] == "connected"
    assert captured["request"] is request
    assert captured["params"].device_id == "cinder"
    assert captured["params"].limit == 8
    assert captured["skip_initial_replay"] is False
    assert captured["owner_id"] == 1


def test_machine_session_delta_is_small_and_contains_no_browser_card_copies():
    session = SimpleNamespace(
        id="633a0114-de2d-4b3d-b1b9-dfa7f314e300",
        device_id="cinder",
        timeline_title="why is the opencode stuck on naming session",
        title_state="ready",
        title_source="prompt",
        runtime_phase="thinking",
        display_phase="Thinking",
        last_activity_at=datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc),
        runtime_version=42,
    )

    payload = project_machine_session_delta(session, commit_seq="91")
    encoded = json.dumps(payload, separators=(",", ":")).encode()

    assert len(encoded) <= 512
    assert payload["session_id"] == session.id
    assert payload["source"] == "runtime_host"
    assert "head" not in payload
    assert "detail" not in payload
    assert "root" not in payload


def test_canonical_machine_session_delta_carries_server_presentation_and_action_state():
    state = SessionStateFacts.model_validate(
        {
            "mode": "helm",
            "disposition": {"state": "open"},
            "run": {"id": "run-1", "lifecycle": "running"},
            "activity": {"state": "unknown", "raw_kind": "quiet"},
            "control": {
                "ownership": "owned",
                "connection": "connected",
                "actions": {
                    "send_input": {"state": "available"},
                    "interrupt": {"state": "available"},
                    "terminate": {"state": "available"},
                    "reattach": {"state": "unavailable", "reason": "already_attached"},
                    "resume": {"state": "unavailable", "reason": "unsupported"},
                },
            },
            "transcript": {"convergence": "current"},
            "host": {"state": "online"},
            "presentation": {
                "primary": {"key": "activity_unknown", "label": "Activity unknown", "tone": "unknown"},
                "access": {"key": "live_control", "label": "Live control", "tone": "control"},
            },
            "commit_seq": 91,
        }
    )
    session = SimpleNamespace(
        id="633a0114-de2d-4b3d-b1b9-dfa7f314e300",
        device_id="cinder",
        timeline_title="Investigate state",
        title_state="ready",
        title_source="ai",
        runtime_phase="quiet",
        display_phase="Idle",
        last_activity_at=datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc),
        runtime_version=91,
        session_state=state,
    )

    payload = project_machine_session_delta(session, commit_seq=91, canonical=True)
    encoded = json.dumps(payload, separators=(",", ":")).encode()

    assert len(encoded) <= 2_048
    assert payload["authority"] == "runtime_host"
    assert payload["presentation"]["primary"]["key"] == "activity_unknown"
    assert payload["presentation"]["access"]["key"] == "live_control"
    assert payload["control"]["actions"]["terminate"]["state"] == "available"
    assert payload["activity"]["state"] == "unknown"
    assert payload["commit_seq"] == "91"
    assert "head" not in payload and "detail" not in payload and "root" not in payload


def test_canonical_machine_stream_initial_replay_preserves_commit_coordinate(monkeypatch):
    state = SessionStateFacts.model_validate(
        {
            "mode": "helm",
            "disposition": {"state": "open"},
            "activity": {"state": "unknown"},
            "control": {
                "ownership": "owned",
                "connection": "unknown",
                "actions": {
                    "send_input": {"state": "unknown", "reason": "control_freshness_unknown"},
                    "interrupt": {"state": "unknown", "reason": "control_freshness_unknown"},
                    "terminate": {"state": "unknown", "reason": "control_freshness_unknown"},
                    "reattach": {"state": "unavailable", "reason": "not_granted"},
                    "resume": {"state": "unavailable", "reason": "not_granted"},
                },
            },
            "transcript": {"convergence": "unknown"},
            "host": {"state": "unknown"},
            "presentation": {
                "primary": {"key": "activity_unknown", "label": "Activity unknown", "tone": "quiet"},
                "access": {"key": "control_unknown", "label": "Control unknown", "tone": "inactive"},
            },
            "commit_seq": 91,
        }
    )
    session = SimpleNamespace(
        id="633a0114-de2d-4b3d-b1b9-dfa7f314e300",
        device_id="cinder",
        timeline_title="Investigate state",
        title_state="ready",
        title_source="ai",
        runtime_phase=None,
        display_phase="Activity unknown",
        last_activity_at=datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc),
        runtime_version=91,
        session_state=state,
    )
    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_DETAIL_SERVE", "canonical")
    monkeypatch.setattr(
        live_catalog_timeline,
        "list_live_catalog_timeline",
        lambda **_kwargs: SimpleNamespace(sessions=[SimpleNamespace(head=session)]),
    )

    async def read_initial_events():
        stream = live_catalog_timeline.stream_live_catalog_machine_sessions(
            _Request(),
            params=SimpleNamespace(device_id=None),
            skip_initial_replay=False,
            owner_id=1,
        )
        return await anext(stream), await anext(stream)

    connected, delta = asyncio.run(read_initial_events())
    assert connected["event"] == "connected"
    assert delta["event"] == "session_delta"
    assert json.loads(delta["data"])["commit_seq"] == "91"
