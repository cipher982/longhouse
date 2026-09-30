"""Device revocation: a machine retiring its own token, and an owner revoking a machine.

`longhouse auth --clear` and `longhouse uninstall` call `DELETE /agents/device-token`
with the token being retired. The owner's Devices page calls
`POST /devices/machines/{device_id}/revoke`, which revokes every live token
issued to that device name (each `longhouse auth` mints a new one).
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.auth.managed_session_tokens import MANAGED_SESSION_SCOPE_HOOK
from zerg.auth.managed_session_tokens import issue_managed_session_token
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.auth import _auth_compat_db
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.device_token import DeviceToken
from zerg.models.models import User
from zerg.routers.device_tokens import generate_device_token
from zerg.routers.device_tokens import hash_token


class _DirectSerializer:
    is_configured = True

    async def execute_or_direct(self, fn, fallback_db, *, label="", auto_commit=True):
        result = fn(fallback_db)
        if auto_commit:
            fallback_db.commit()
        return result


def _agents_settings():
    return SimpleNamespace(auth_disabled=False, testing=True, single_tenant=True, environment="test")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A test DB with owners 1 and 2, the routes wired to it, and owner 1 signed in."""
    engine = make_engine(f"sqlite:///{tmp_path / 'revoke.db'}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def _override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[_auth_compat_db] = _override_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="alice@example.com", role="ADMIN")
    with factory() as db:
        db.add_all([User(id=1, email="alice@example.com", role="ADMIN"), User(id=2, email="bob@example.com", role="ADMIN")])
        db.commit()

    monkeypatch.setattr("zerg.dependencies.agents_auth.get_settings", _agents_settings)
    monkeypatch.setattr("zerg.dependencies.agents_auth.get_session_factory", lambda: factory)
    monkeypatch.setattr("zerg.routers.device_tokens.get_write_serializer", lambda: _DirectSerializer())

    def seed(owner_id: int, device_id: str, *, revoked: bool = False) -> str:
        plain = generate_device_token()
        with factory() as db:
            db.add(
                DeviceToken(
                    owner_id=owner_id,
                    device_id=device_id,
                    token_hash=hash_token(plain),
                    revoked_at=datetime.now(UTC) if revoked else None,
                )
            )
            db.commit()
        return plain

    def valid(plain: str) -> bool:
        with factory() as db:
            row = db.query(DeviceToken).filter(DeviceToken.token_hash == hash_token(plain)).one()
            return row.revoked_at is None

    yield SimpleNamespace(client=TestClient(api_app), seed=seed, valid=valid)

    for dependency in (get_db, _auth_compat_db, get_current_user):
        api_app.dependency_overrides.pop(dependency, None)


def _agents_request(token: str) -> Request:
    headers = [(b"x-agents-token", token.encode())]
    return Request({"type": "http", "method": "GET", "path": "/api/agents/sessions", "headers": headers, "query_string": b""})


def _accepted_by_agents_api(token: str) -> bool:
    try:
        verify_agents_token(_agents_request(token))
    except HTTPException as exc:
        assert exc.status_code == 401
        return False
    return True


# ---------------------------------------------------------------------------
# DELETE /agents/device-token: a machine retires the token it presents
# ---------------------------------------------------------------------------


def test_a_device_revokes_the_token_it_presents_and_the_agents_api_then_refuses_it(world):
    mine = world.seed(1, "macbook")
    sibling = world.seed(1, "macbook")
    assert _accepted_by_agents_api(mine)

    response = world.client.delete("/agents/device-token", headers={"X-Agents-Token": mine})

    assert response.status_code == 204, response.text
    assert not world.valid(mine)
    assert not _accepted_by_agents_api(mine)
    # Exactly the presented token: another token for the same device stays live.
    assert world.valid(sibling)
    assert _accepted_by_agents_api(sibling)


def test_revoking_an_already_revoked_token_is_401_not_a_second_success(world):
    token = world.seed(1, "macbook")
    assert world.client.delete("/agents/device-token", headers={"X-Agents-Token": token}).status_code == 204
    assert world.client.delete("/agents/device-token", headers={"X-Agents-Token": token}).status_code == 401

    already = world.seed(1, "cinder", revoked=True)
    assert world.client.delete("/agents/device-token", headers={"X-Agents-Token": already}).status_code == 401
    assert world.client.delete("/agents/device-token", headers={"X-Agents-Token": generate_device_token()}).status_code == 401


def test_self_revoke_refuses_a_missing_or_non_device_credential_and_revokes_nothing(world):
    token = world.seed(1, "macbook")
    managed = issue_managed_session_token(
        owner_id=1,
        session_id=str(uuid4()),
        project=None,
        device_id="macbook",
        scope=MANAGED_SESSION_SCOPE_HOOK,
    )

    assert world.client.delete("/agents/device-token").status_code == 401
    assert world.client.delete("/agents/device-token", headers={"X-Agents-Token": managed}).status_code == 403
    # A browser bearer credential is not the machine header either.
    assert world.client.delete("/agents/device-token", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert world.valid(token)


def test_self_revoke_needs_a_token_even_when_auth_is_disabled(world, monkeypatch):
    token = world.seed(1, "macbook")
    monkeypatch.setattr(
        "zerg.dependencies.agents_auth.get_settings",
        lambda: SimpleNamespace(auth_disabled=True, testing=True, single_tenant=True, environment="test"),
    )

    assert world.client.delete("/agents/device-token").status_code == 401
    assert world.valid(token)


def test_self_revoke_goes_through_the_catalog_revoke_op_with_the_tokens_own_owner_and_id():
    token_id = uuid4()
    observed: dict = {}
    presented = DeviceToken(id=str(token_id), owner_id=7, device_id="cinder", token_hash="0" * 64)

    class _CatalogClient:
        async def call(self, method, params, *, timeout_seconds):
            observed.update(method=method, params=params, timeout_seconds=timeout_seconds)
            return {"found": True, "changed": True, "token_id": params["token_id"], "commit_seq": "3"}

    with (
        patch("zerg.dependencies.agents_auth._validate_device_token_for_request", return_value=presented),
        patch("zerg.routers.device_tokens.live_store_configured", return_value=True),
        patch("zerg.routers.device_tokens.get_settings", return_value=SimpleNamespace(testing=False)),
        patch("zerg.services.catalogd_supervisor.get_catalogd_client", return_value=_CatalogClient()),
        patch("zerg.routers.device_tokens.get_write_serializer", side_effect=AssertionError("must not mutate in API")),
    ):
        response = TestClient(api_app).delete("/agents/device-token", headers={"X-Agents-Token": "zdt_presented"})

    assert response.status_code == 204, response.text
    assert observed == {
        "method": "auth.device.revoke.v2",
        "params": {"owner_id": 7, "token_id": str(token_id)},
        "timeout_seconds": 1.0,
    }


# ---------------------------------------------------------------------------
# POST /devices/machines/{device_id}/revoke: the owner revokes a machine
# ---------------------------------------------------------------------------


def test_revoking_a_machine_revokes_every_live_token_for_that_name_and_only_those(world):
    older = world.seed(1, "macbook")
    newer = world.seed(1, "macbook")
    already = world.seed(1, "macbook", revoked=True)
    other_device = world.seed(1, "cinder")
    other_owner = world.seed(2, "macbook")

    response = world.client.post("/devices/machines/macbook/revoke")

    assert response.status_code == 200, response.text
    assert response.json() == {"device_id": "macbook", "revoked": 2}
    assert not world.valid(older) and not world.valid(newer)
    assert not _accepted_by_agents_api(older) and not _accepted_by_agents_api(newer)
    assert not world.valid(already)
    assert world.valid(other_device) and _accepted_by_agents_api(other_device)
    assert world.valid(other_owner) and _accepted_by_agents_api(other_owner)


def test_revoking_a_machine_with_no_live_token_is_a_200_with_zero(world):
    world.seed(1, "cinder")
    world.seed(1, "macbook", revoked=True)

    assert world.client.post("/devices/machines/unknown/revoke").json() == {"device_id": "unknown", "revoked": 0}
    assert world.client.post("/devices/machines/macbook/revoke").json() == {"device_id": "macbook", "revoked": 0}
    # And it is repeatable.
    first = world.client.post("/devices/machines/cinder/revoke").json()
    assert first["revoked"] == 1
    assert world.client.post("/devices/machines/cinder/revoke").json() == {"device_id": "cinder", "revoked": 0}


def test_revoking_a_machine_uses_the_existing_catalog_list_and_revoke_ops():
    ids = [str(uuid4()) for _ in range(3)]
    now = datetime.now(UTC).isoformat()
    calls: list[tuple[str, dict]] = []

    def _payload(token_id: str, device_id: str) -> dict:
        return {
            "id": token_id,
            "device_id": device_id,
            "created_at": now,
            "last_used_at": None,
            "revoked_at": None,
            "is_valid": True,
        }

    class _CatalogClient:
        async def call(self, method, params, *, timeout_seconds=None):
            calls.append((method, params))
            if method == "auth.device.list.v2":
                return {"tokens": [_payload(ids[0], "macbook"), _payload(ids[1], "cinder"), _payload(ids[2], "macbook")]}
            return {"found": True, "changed": True, "token_id": params["token_id"], "commit_seq": "5"}

    with (
        patch("zerg.routers.device_tokens.live_store_configured", return_value=True),
        patch("zerg.routers.device_tokens.get_settings", return_value=SimpleNamespace(testing=False)),
        patch("zerg.services.catalogd_supervisor.get_catalogd_client", return_value=_CatalogClient()),
        patch("zerg.routers.device_tokens.get_write_serializer", side_effect=AssertionError("must not mutate in API")),
    ):
        api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="alice@example.com", role="ADMIN")
        try:
            response = TestClient(api_app).post("/devices/machines/macbook/revoke")
        finally:
            api_app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200, response.text
    assert response.json() == {"device_id": "macbook", "revoked": 2}
    assert calls == [
        ("auth.device.list.v2", {"owner_id": 1, "include_revoked": False}),
        ("auth.device.revoke.v2", {"owner_id": 1, "token_id": ids[0]}),
        ("auth.device.revoke.v2", {"owner_id": 1, "token_id": ids[2]}),
    ]
