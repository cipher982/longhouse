"""Focused tests for Gmail watch renewal state handling."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import Connector
from zerg.models import User
from zerg.services.watch_renewal_service import WatchRenewalService
from zerg.utils.crypto import encrypt


def _make_db(tmp_path):
    db_path = tmp_path / "test_gmail_watch_renewal.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1, email: str = "owner@example.com") -> User:
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_connector(db, *, owner_id: int, config: dict[str, object]) -> Connector:
    connector = Connector(owner_id=owner_id, type="email", provider="gmail", config=config)
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


@pytest.mark.asyncio
async def test_watch_renewal_marks_connector_not_configured_without_pubsub_topic(tmp_path):
    session_local = _make_db(tmp_path)
    watch_expiry = int((time.time() + 60) * 1000)

    with session_local() as db:
        owner = _seed_user(db)
        seeded = _seed_connector(
            db,
            owner_id=owner.id,
            config={
                "refresh_token": encrypt("refresh-token"),
                "emailAddress": "owner@gmail.com",
                "history_id": 100,
                "watch_expiry": watch_expiry,
            },
        )
        connector_id = seeded.id

    settings = SimpleNamespace(testing=False, gmail_pubsub_topic=None, app_public_url="https://example.com")
    start_watch = Mock()

    with session_local() as db:
        connector = db.get(Connector, connector_id)
        assert connector is not None

        with (
            patch("zerg.config.get_settings", return_value=settings),
            patch("zerg.services.gmail_api.async_exchange_refresh_token", AsyncMock(return_value="access-token")),
            patch("zerg.services.gmail_api.start_watch", start_watch),
        ):
            now = time.time()
            renewal_threshold = now + (24 * 3600)
            await WatchRenewalService()._process_connector_renewal(db, connector, now, renewal_threshold)

        db.expire_all()
        refreshed = db.get(Connector, connector_id)
        assert refreshed is not None
        assert refreshed.config["history_id"] == 100
        assert refreshed.config["watch_expiry"] == watch_expiry
        assert refreshed.config["watch_status"] == "not_configured"
        assert refreshed.config["watch_method"] is None
        assert "GMAIL_PUBSUB_TOPIC" in refreshed.config["watch_error"]
        start_watch.assert_not_called()


@pytest.mark.asyncio
async def test_watch_renewal_uses_pubsub_and_updates_connector_state(tmp_path):
    session_local = _make_db(tmp_path)
    watch_expiry = int((time.time() + 60) * 1000)

    with session_local() as db:
        owner = _seed_user(db)
        seeded = _seed_connector(
            db,
            owner_id=owner.id,
            config={
                "refresh_token": encrypt("refresh-token"),
                "emailAddress": "owner@gmail.com",
                "history_id": 100,
                "watch_expiry": watch_expiry,
                "watch_status": "active",
            },
        )
        connector_id = seeded.id

    settings = SimpleNamespace(testing=False, gmail_pubsub_topic="projects/demo/topics/gmail", app_public_url=None)
    start_watch = Mock(return_value={"history_id": 222, "watch_expiry": 333})

    with session_local() as db:
        connector = db.get(Connector, connector_id)
        assert connector is not None

        with (
            patch("zerg.config.get_settings", return_value=settings),
            patch("zerg.services.gmail_api.async_exchange_refresh_token", AsyncMock(return_value="access-token")),
            patch("zerg.services.gmail_api.start_watch", start_watch),
        ):
            now = time.time()
            renewal_threshold = now + (24 * 3600)
            await WatchRenewalService()._process_connector_renewal(db, connector, now, renewal_threshold)

        db.expire_all()
        refreshed = db.get(Connector, connector_id)
        assert refreshed is not None
        assert refreshed.config["history_id"] == 222
        assert refreshed.config["watch_expiry"] == 333
        assert refreshed.config["watch_status"] == "active"
        assert refreshed.config["watch_method"] == "pubsub"
        assert refreshed.config["watch_error"] is None
        start_watch.assert_called_once_with(access_token="access-token", topic_name="projects/demo/topics/gmail")
