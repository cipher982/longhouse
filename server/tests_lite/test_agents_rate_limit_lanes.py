from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli.sessions import _parse_retry_after
from zerg.cli.sessions import continue_session
from zerg.dependencies.agents_auth import _rate_buckets
from zerg.dependencies.agents_auth import _rate_limit_lane
from zerg.dependencies.agents_auth import _rate_lock
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.device_token import DeviceToken


def _req(method: str, path: str, token: str = "zdt_test_token") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"x-agents-token", token.encode())],
            "query_string": b"",
        }
    )


def test_rate_limit_lane_classification():
    # Read lane
    assert _rate_limit_lane(_req("GET", "/api/agents/sessions/stream")) == "read"
    assert _rate_limit_lane(_req("GET", "/api/agents/sessions/123/tail")) == "read"
    assert _rate_limit_lane(_req("HEAD", "/api/agents/storage/v2/media/abc")) == "read"

    # Control lane (session steering and direct interaction)
    assert _rate_limit_lane(_req("POST", "/api/agents/sessions/123/send-live")) == "control"
    assert _rate_limit_lane(_req("POST", "/api/agents/sessions/123/interrupt-live")) == "control"
    assert _rate_limit_lane(_req("POST", "/api/agents/sessions/123/turns/current/interrupt")) == "control"
    assert _rate_limit_lane(_req("POST", "/api/agents/sessions/123/terminate-live")) == "control"
    assert _rate_limit_lane(_req("POST", "/api/agents/sessions/123/inputs")) == "control"
    assert _rate_limit_lane(_req("POST", "/api/agents/directed-inputs")) == "control"
    assert _rate_limit_lane(_req("POST", "/api/agents/directed-inputs/123/reply")) == "control"
    assert _rate_limit_lane(_req("POST", "/api/agents/sessions/123/pause-responses")) == "control"

    # Ingest lane (high-frequency background agent traffic)
    assert _rate_limit_lane(_req("POST", "/api/agents/runtime/events/batch")) == "ingest"
    assert _rate_limit_lane(_req("POST", "/api/agents/presence")) == "ingest"
    assert _rate_limit_lane(_req("POST", "/api/agents/heartbeat")) == "ingest"

    # Storage lane (storage-v2 writes admitted by resource-aware backpressure)
    assert _rate_limit_lane(_req("POST", "/api/agents/storage/v2/envelopes")) == "storage"
    assert _rate_limit_lane(_req("POST", "/api/agents/storage/v2/media/claims")) == "storage"
    assert _rate_limit_lane(_req("PUT", "/api/agents/storage/v2/media/" + "a" * 64)) == "storage"


def test_control_lane_isolated_from_ingest_floods(monkeypatch):
    device_id = str(uuid4())
    fake_token = DeviceToken(id=device_id, owner_id=1, device_id="macbook", token_hash="h")

    monkeypatch.setattr(
        "zerg.dependencies.agents_auth._validate_device_token_for_request",
        lambda token: fake_token,
    )
    monkeypatch.setattr(
        "zerg.dependencies.agents_auth.get_settings",
        lambda: SimpleNamespace(auth_disabled=False, testing=False, single_tenant=True),
    )
    monkeypatch.setattr("zerg.dependencies.agents_auth._RATE_LIMIT_MAX_REQUESTS", 2)
    monkeypatch.setattr("zerg.dependencies.agents_auth._RATE_LIMIT_WINDOW_SECONDS", 60.0)

    with _rate_lock:
        _rate_buckets.clear()

    # Saturate the ingest bucket (limit = 2)
    req_ingest1 = _req("POST", "/api/agents/runtime/events/batch")
    verify_agents_token(req_ingest1)
    assert req_ingest1.state.agents_rate_key == f"device:{device_id}:ingest"

    req_ingest2 = _req("POST", "/api/agents/runtime/events/batch")
    verify_agents_token(req_ingest2)

    # 3rd ingest request is 429 rate-limited
    req_ingest3 = _req("POST", "/api/agents/runtime/events/batch")
    with pytest.raises(HTTPException) as exc_info:
        verify_agents_token(req_ingest3)
    assert exc_info.value.status_code == 429

    # Control request (send-live) uses its own bucket and SUCCEEDS despite saturated ingest
    req_control = _req("POST", f"/api/agents/sessions/{uuid4()}/send-live")
    resolved_control = verify_agents_token(req_control)
    assert resolved_control is fake_token
    assert req_control.state.agents_rate_key == f"device:{device_id}:control"

    # Read request (tail) also uses its own bucket and SUCCEEDS
    req_read = _req("GET", f"/api/agents/sessions/{uuid4()}/tail")
    resolved_read = verify_agents_token(req_read)
    assert resolved_read is fake_token
    assert req_read.state.agents_rate_key == f"device:{device_id}:read"


def test_history_import_writes_do_not_consume_the_ingest_bucket(monkeypatch):
    """A first import is thousands of storage-v2 writes; request counting must not
    pace it, and it must not starve the machine's live runtime events."""
    device_id = str(uuid4())
    fake_token = DeviceToken(id=device_id, owner_id=1, device_id="macbook", token_hash="h")
    monkeypatch.setattr(
        "zerg.dependencies.agents_auth._validate_device_token_for_request",
        lambda token: fake_token,
    )
    monkeypatch.setattr(
        "zerg.dependencies.agents_auth.get_settings",
        lambda: SimpleNamespace(auth_disabled=False, testing=False, single_tenant=True),
    )
    monkeypatch.setattr("zerg.dependencies.agents_auth._RATE_LIMIT_MAX_REQUESTS", 2)
    monkeypatch.setattr("zerg.dependencies.agents_auth._RATE_LIMIT_WINDOW_SECONDS", 60.0)
    with _rate_lock:
        _rate_buckets.clear()

    for path, method in [
        ("/api/agents/storage/v2/envelopes", "POST"),
        ("/api/agents/storage/v2/media/claims", "POST"),
        ("/api/agents/storage/v2/media/" + "b" * 64, "PUT"),
    ] * 10:
        request = _req(method, path)
        assert verify_agents_token(request) is fake_token
        assert request.state.agents_rate_key == f"device:{device_id}:storage"

    with _rate_lock:
        assert f"device:{device_id}:storage" not in _rate_buckets
    for _ in range(2):
        verify_agents_token(_req("POST", "/api/agents/runtime/events/batch"))
    with pytest.raises(HTTPException) as exc_info:
        verify_agents_token(_req("POST", "/api/agents/runtime/events/batch"))
    assert exc_info.value.status_code == 429


def test_parse_retry_after():
    resp_no_header = httpx.Response(429)
    assert _parse_retry_after(resp_no_header, default=2.0) == 2.0

    resp_valid = httpx.Response(429, headers={"Retry-After": "3"})
    assert _parse_retry_after(resp_valid) == 3.0

    resp_float = httpx.Response(429, headers={"Retry-After": "1.5"})
    assert _parse_retry_after(resp_float) == 1.5

    # Clamped to min 0.5s and max 5.0s
    resp_too_low = httpx.Response(429, headers={"Retry-After": "0.1"})
    assert _parse_retry_after(resp_too_low) == 0.5

    resp_too_high = httpx.Response(429, headers={"Retry-After": "120"})
    assert _parse_retry_after(resp_too_high) == 5.0

    resp_invalid = httpx.Response(429, headers={"Retry-After": "invalid"})
    assert _parse_retry_after(resp_invalid, default=1.0) == 1.0


def test_continue_session_retries_on_429(monkeypatch):
    session_id = str(uuid4())
    monkeypatch.setattr("zerg.cli.sessions._load_api_credentials", lambda **kwargs: ("http://test", "zdt_tok"))

    attempts = 0

    @contextmanager
    def mock_stream(method, url, headers=None, json=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # First attempt: rate limited with 429
            yield httpx.Response(
                429,
                headers={"Retry-After": "1"},
                content=b'{"detail":"Rate limit exceeded for agents API token."}',
                request=httpx.Request(method, url),
            )
        else:
            # Second attempt: success with 200
            yield httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"accepted":true,"session_id":"test-session","dispatch_ms":42}',
                request=httpx.Request(method, url),
            )

    client_mock = MagicMock()
    client_mock.__enter__.return_value = client_mock
    client_mock.stream = mock_stream

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_mock)
    monkeypatch.setattr("time.sleep", lambda s: None)

    # Should succeed after 1 retry without raising SystemExit/typer.Exit
    continue_session(
        session_id=session_id,
        message="hello peer",
        current_session_id=None,
        url=None,
        token=None,
        claude_dir=None,
    )


def test_interrupt_retries_on_429(monkeypatch):
    session_id = str(uuid4())
    monkeypatch.setattr("zerg.cli.sessions._load_api_credentials", lambda **kwargs: ("http://test", "zdt_tok"))

    attempts = 0

    def mock_post(url, headers=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "1"},
                content=b'{"detail":"Rate limit exceeded for agents API token."}',
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"interrupt_dispatched":true,"session_id":"test-session"}',
            request=httpx.Request("POST", url),
        )

    client_mock = MagicMock()
    client_mock.__enter__.return_value = client_mock
    client_mock.post = mock_post

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_mock)
    monkeypatch.setattr("time.sleep", lambda s: None)

    from zerg.cli.sessions import interrupt

    interrupt(
        session_id=session_id,
        current_session_id=None,
        url=None,
        token=None,
        claude_dir=None,
    )
    assert attempts == 2
