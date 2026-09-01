"""The router boundary always resolves a non-optional owner identity."""

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.auth.caller import Caller
from zerg.auth.caller import caller_principal
from zerg.dependencies import agents_auth
from zerg.dependencies.browser_auth import get_current_browser_caller
from zerg.dependencies.browser_route_auth import get_current_browser_route_caller


def test_machine_caller_keeps_credential_specific_principal():
    principal = SimpleNamespace(owner_id=7, device_id="macbook")

    caller = agents_auth.verify_agents_caller(principal)

    assert caller == Caller(owner_id=7, principal=principal)
    assert caller.device_id == "macbook"
    assert caller_principal(caller) is principal


def test_browser_boundaries_return_the_same_caller_type():
    user = SimpleNamespace(id=11)

    assert get_current_browser_caller(user) == Caller(owner_id=11, principal=user)
    assert get_current_browser_route_caller(user) == Caller(owner_id=11, principal=user)


def test_machine_caller_fails_closed_without_an_owner(monkeypatch):
    monkeypatch.setattr("zerg.services.catalog_read_gateway.active_owner_id", lambda: None)

    with pytest.raises(HTTPException) as error:
        agents_auth.verify_agents_caller(None)

    assert error.value.status_code == 503
