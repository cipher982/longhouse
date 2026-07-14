from __future__ import annotations

import asyncio
from types import SimpleNamespace

import zerg.routers.agents_sessions as agents_sessions


class _Request:
    async def is_disconnected(self) -> bool:
        return True


def test_machine_session_stream_uses_commit_driven_catalog_projection(monkeypatch):
    captured = {}

    async def fake_stream(request, *, params, skip_initial_replay):
        captured["request"] = request
        captured["params"] = params
        captured["skip_initial_replay"] = skip_initial_replay
        yield {"event": "connected", "data": "{}"}

    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "stream_live_catalog_timeline", fake_stream)
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
