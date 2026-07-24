from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.auth.managed_session_tokens import MANAGED_SESSION_SCOPE_COORDINATION
from zerg.auth.managed_session_tokens import MANAGED_SESSION_SCOPE_HOOK
from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.auth.managed_session_tokens import issue_managed_session_token
from zerg.auth.managed_session_tokens import validate_managed_session_token
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.routers.agents_sessions import _resolve_directed_input_actor


def _request(method: str, path: str, token: str, *, session_id: str | None = None) -> Request:
    headers = [(b"x-agents-token", token.encode())]
    if session_id is not None:
        headers.append((b"x-longhouse-session-id", session_id.encode()))
    return Request({"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""})


def _settings():
    return SimpleNamespace(auth_disabled=False, testing=True, single_tenant=True)


def _token(*, session_id: str, scope: str) -> str:
    return issue_managed_session_token(
        owner_id=7,
        session_id=session_id,
        project="longhouse",
        device_id="device-7",
        scope=scope,
    )


def test_managed_session_token_requires_explicit_valid_scope():
    session_id = str(uuid4())

    with pytest.raises(ValueError, match="scope"):
        _token(session_id=session_id, scope="admin")

    expired = issue_managed_session_token(
        owner_id=7,
        session_id=session_id,
        project=None,
        device_id=None,
        scope=MANAGED_SESSION_SCOPE_COORDINATION,
        expires_delta=timedelta(seconds=-1),
    )
    assert validate_managed_session_token(expired) is None


def test_coordination_scope_can_only_reach_directed_input_routes(monkeypatch):
    session_id = str(uuid4())
    token = _token(session_id=session_id, scope=MANAGED_SESSION_SCOPE_COORDINATION)
    monkeypatch.setattr("zerg.dependencies.agents_auth.get_settings", _settings)

    resolved = verify_agents_token(_request("POST", "/api/agents/directed-inputs", token))
    assert isinstance(resolved, ManagedSessionToken)
    assert resolved.scope == MANAGED_SESSION_SCOPE_COORDINATION

    with pytest.raises(HTTPException) as denied:
        verify_agents_token(_request("POST", "/api/agents/permission-requests", token))
    assert denied.value.status_code == 403


def test_hook_scope_cannot_reach_directed_input_routes(monkeypatch):
    session_id = str(uuid4())
    token = _token(session_id=session_id, scope=MANAGED_SESSION_SCOPE_HOOK)
    monkeypatch.setattr("zerg.dependencies.agents_auth.get_settings", _settings)

    resolved = verify_agents_token(_request("POST", "/api/agents/permission-requests", token))
    assert isinstance(resolved, ManagedSessionToken)
    assert resolved.scope == MANAGED_SESSION_SCOPE_HOOK

    with pytest.raises(HTTPException) as denied:
        verify_agents_token(_request("GET", "/api/agents/directed-inputs", token))
    assert denied.value.status_code == 403


def test_directed_actor_rejects_device_authority_even_with_session_header():
    session_id = str(uuid4())
    request = _request("POST", "/api/agents/directed-inputs", "zdt_device", session_id=session_id)

    with pytest.raises(HTTPException) as denied:
        _resolve_directed_input_actor(
            db=None,
            request=request,
            token=SimpleNamespace(owner_id=7, device_id="device-7"),
        )
    assert denied.value.status_code == 403
    assert "session-scoped coordination authority" in str(denied.value.detail)


def test_directed_actor_binds_signed_identity_not_ambient_header(monkeypatch):
    signed_session_id = str(uuid4())
    other_session_id = str(uuid4())
    token = ManagedSessionToken(
        owner_id=7,
        session_id=signed_session_id,
        device_id="device-7",
        scope=MANAGED_SESSION_SCOPE_COORDINATION,
    )
    request = _request(
        "POST",
        "/api/agents/directed-inputs",
        "zst_test",
        session_id=other_session_id,
    )
    monkeypatch.setattr("zerg.routers.agents_sessions.database_module.live_catalog_enabled", lambda: True)

    with pytest.raises(HTTPException) as denied:
        _resolve_directed_input_actor(db=None, request=request, token=token)
    assert denied.value.status_code == 403
    assert "does not match request header" in str(denied.value.detail)
