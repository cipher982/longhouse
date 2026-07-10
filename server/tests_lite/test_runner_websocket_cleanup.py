import logging

import pytest
from fastapi import WebSocketDisconnect

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.routers.runners import _runner_websocket_with_db
from zerg.routers.runners import runner_websocket


def _make_db(tmp_path):
    db_path = tmp_path / "test_runner_websocket_cleanup.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


class _DisconnectBeforeHelloWebSocket:
    def __init__(self):
        self.accepted = False
        self.close_calls = 0

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        raise WebSocketDisconnect(code=1006)

    async def close(self, code=1000, reason=None):
        self.close_calls += 1
        raise RuntimeError("Unexpected ASGI message 'websocket.close', after sending 'websocket.close' or response already completed.")


class _InvalidHelloWebSocket:
    def __init__(self):
        self.accepted = False
        self.close_calls = 0

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        raise RuntimeError("boom")

    async def close(self, code=1000, reason=None):
        self.close_calls += 1
        raise RuntimeError("Unexpected ASGI message 'websocket.close', after sending 'websocket.close' or response already completed.")


class _CatalogModeWebSocket:
    query_params = {}

    def __init__(self):
        self.closed = None

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_runner_disconnect_before_hello_is_quiet(tmp_path, caplog):
    SessionLocal = _make_db(tmp_path)
    websocket = _DisconnectBeforeHelloWebSocket()

    caplog.set_level(logging.INFO, logger="zerg.routers.runners")

    with SessionLocal() as db:
        await _runner_websocket_with_db(websocket, db)

    assert websocket.accepted is True
    assert "Runner disconnected before hello" in caplog.text
    assert "Failed to receive hello message" not in caplog.text
    assert "Error in runner websocket handler" not in caplog.text


@pytest.mark.asyncio
async def test_runner_invalid_hello_close_race_is_swallowed(tmp_path, caplog):
    SessionLocal = _make_db(tmp_path)
    websocket = _InvalidHelloWebSocket()

    caplog.set_level(logging.WARNING, logger="zerg.routers.runners")

    with SessionLocal() as db:
        await _runner_websocket_with_db(websocket, db)

    assert websocket.accepted is True
    assert "Failed to receive hello message: boom" in caplog.text
    assert "Error in runner websocket handler" not in caplog.text


@pytest.mark.asyncio
async def test_runner_websocket_does_not_open_archive_in_catalog_mode(monkeypatch):
    websocket = _CatalogModeWebSocket()
    monkeypatch.setattr("zerg.routers.runners.live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.routers.runners.get_session_factory", lambda: pytest.fail("cold DB opened"))

    await runner_websocket(websocket)

    assert websocket.closed is not None
    assert websocket.closed[0] == 1013
