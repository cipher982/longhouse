"""The access log must name who is on the other end of a WebSocket.

Live transcript streaming is the highest-value thing in this system to audit,
and it travels over WebSockets. Both sockets that authenticate from the
handshake resolve their caller *before* ``accept()`` and then threw that
identity away for logging purposes, so every one of those lines read
``principal=unattributed`` -- the exact question the access log exists to
answer, unanswered on the exact traffic that most needs it.

The fix is a stamp, not a reordering: ``scope["state"]["principal"]`` is the
only thing ``AccessLogMiddleware`` reads, and a write through
``websocket.state`` inside the mounted ``api_app`` lands in the same scope dict
the outer middleware sees.

``/api/runners/ws`` is deliberately different and this file pins that too. Its
secret arrives in the ``hello`` frame, after accept, so at accept the server
genuinely does not know the caller and ``unattributed`` is the honest label.
It emits a second, attributed line once the frame authenticates. Stamping a
guess there would be worse than the gap.

This drives the real outer ``app`` -- the middleware is installed there, not on
``api_app`` -- so the assertions cover the wire paths as the middleware sees
them, mount prefix included.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "lh-ws-principal-tests-secret")
os.environ.setdefault("INTERNAL_API_SECRET", "lh-test-internal")
os.environ.setdefault("GOOGLE_CLIENT_ID", "lh-test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "lh-test-google-client")

from zerg.main import app  # noqa: E402

ACCESS_LOGGER = "zerg.middleware.access_log"

# Deliberately implausible as a real row id: if an assertion below ever passes
# by reading someone else's stamp, the number makes that obvious.
BROWSER_USER_ID = 90210
DEVICE_TOKEN_ID = 60453
RUNNER_ID = 70714


def _access_lines(caplog) -> list[tuple[str, str]]:
    """(message, principal) for every access-log record, in order."""
    return [
        (record.getMessage(), getattr(record, "principal", "<no principal attribute>"))
        for record in caplog.records
        if record.name == ACCESS_LOGGER
    ]


def _principal_for(caplog, message: str) -> str:
    matches = [principal for line, principal in _access_lines(caplog) if line == message]
    assert matches, f"no access-log line {message!r}; got {_access_lines(caplog)}"
    return matches[0]


def test_browser_stream_logs_the_authenticated_user(monkeypatch, caplog):
    """``/api/ws`` names the user its own auth call already resolved."""

    monkeypatch.setattr(
        "zerg.routers.websocket.validate_ws_jwt",
        lambda token, *args, **kwargs: SimpleNamespace(id=BROWSER_USER_ID, email="ws-principal@example.com"),
    )

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
        with client.websocket_connect("/api/ws"):
            pass

    assert _principal_for(caplog, "WS /api/ws accepted") == f"user:{BROWSER_USER_ID}"


def test_machine_control_socket_logs_the_device_token(monkeypatch, caplog):
    """``/api/agents/control/ws`` names the device token it validated.

    Same ``device:<token id>`` format the HTTP machine surface stamps, so one
    machine's control socket and its HTTP calls line up in the same log.
    """

    monkeypatch.setattr(
        "zerg.routers.agents_control._validate_websocket_device_token",
        lambda websocket: SimpleNamespace(id=DEVICE_TOKEN_ID, device_id="ws-principal-device"),
    )

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
        with client.websocket_connect("/api/agents/control/ws"):
            pass

    assert _principal_for(caplog, "WS /api/agents/control/ws accepted") == f"device:{DEVICE_TOKEN_ID}"


def test_rejected_handshake_stays_unattributed(monkeypatch, caplog):
    """A refused connection has no principal, and must not borrow one.

    ``unattributed`` is a fact about the connection, not a formatting default.
    A stamp that fired before the auth decision would label rejected traffic
    with whoever it was pretending to be.
    """

    monkeypatch.setattr("zerg.routers.websocket.validate_ws_jwt", lambda token, *args, **kwargs: None)

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/ws"):
                pass

    assert _principal_for(caplog, "WS /api/ws rejected") == "unattributed"


def test_runner_socket_is_unattributed_at_accept_then_attributed_after_hello(monkeypatch, caplog):
    """The one socket that authenticates in-band gets two honest lines.

    ``/api/runners/ws`` accepts before the ``hello`` frame carrying the secret
    arrives, so the accept-time line cannot name anyone -- and says so. The
    deferred line binds the runner identity to the same path and client IP once
    the frame authenticates.
    """

    runner = SimpleNamespace(id=RUNNER_ID, owner_id=4, name="ws-principal-runner")
    monkeypatch.setattr(
        "zerg.routers.runners.authenticate_runner_identity",
        lambda db, **kwargs: SimpleNamespace(authenticated=True, runner=runner, reason_code=None, summary="ok"),
    )
    # The handler marks the runner online/offline through the catalog when it
    # holds no Session; this test is about the log line, not that write.
    monkeypatch.setattr("zerg.crud.runner_crud.update_runner_connection", lambda *args, **kwargs: runner)

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
        with client.websocket_connect("/api/runners/ws") as socket:
            socket.send_json(
                {
                    "type": "hello",
                    "runner_id": RUNNER_ID,
                    "secret": "not-checked-here",
                    "metadata": {},
                }
            )
            # Leaving the block closes the socket and blocks until the handler
            # returns, so the deferred line is written before caplog is read.

    assert _principal_for(caplog, "WS /api/runners/ws accepted") == "unattributed"
    assert _principal_for(caplog, "WS /api/runners/ws authenticated") == f"runner:{RUNNER_ID}"
