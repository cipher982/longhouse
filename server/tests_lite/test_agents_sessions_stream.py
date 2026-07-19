from __future__ import annotations

import asyncio
import json
from builtins import anext
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

import zerg.routers.agents_sessions as agents_sessions
from zerg.services.live_catalog_timeline import project_machine_session_delta


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
