from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from zerg.auth.cp_jwks import CPTokenClaims
from zerg.auth.strategy import HostedCPAuthStrategy
from zerg.database import Base
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.models import User


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _claims(*, cp_user_id: int, email: str, email_verified: bool = True) -> CPTokenClaims:
    return CPTokenClaims(
        cp_user_id=cp_user_id,
        email=email,
        email_verified=email_verified,
        display_name="CP User",
        avatar_url=None,
        audience="david010",
        issuer="https://control.longhouse.ai",
        expires_at=9999999999,
    )


def test_verified_cp_email_can_link_existing_hosted_user(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(email="david010@gmail.com")
    db_session.add(user)
    db_session.commit()

    resolved = strategy._resolve_claims_user(  # noqa: SLF001
        db_session,
        _claims(cp_user_id=123, email="david010@gmail.com", email_verified=True),
    )

    assert resolved.id == user.id
    assert resolved.cp_user_id == 123
    assert resolved.provider == "control-plane"
    assert resolved.email_verified is True


def test_unverified_cp_email_cannot_link_existing_hosted_user(monkeypatch, db_session):
    monkeypatch.setenv("INSTANCE_ID", "david010")
    strategy = HostedCPAuthStrategy()
    user = User(email="david010@gmail.com")
    db_session.add(user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        strategy._resolve_claims_user(  # noqa: SLF001
            db_session,
            _claims(cp_user_id=456, email="david010@gmail.com", email_verified=False),
        )

    assert exc.value.status_code == 403
    db_session.refresh(user)
    assert user.cp_user_id is None


def test_hosted_browser_route_rejects_query_jwt(monkeypatch, db_session):
    monkeypatch.setattr(
        "zerg.dependencies.browser_route_auth.get_settings",
        lambda: SimpleNamespace(control_plane_url="https://control.longhouse.ai"),
    )
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})

    with pytest.raises(HTTPException) as exc:
        get_current_browser_route_user(request, db_session, token="header.payload.signature")

    assert exc.value.status_code == 401
