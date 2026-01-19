"""Tests for connector-level Gmail watch renewal service."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_watch_renewal_updates_connector(db_session, test_user, monkeypatch):
    """Expiring connector watches should be renewed and persisted."""

    from zerg.crud import crud
    from zerg.services import watch_renewal_service as wrs
    from zerg.utils import crypto
    import zerg.config as config_mod

    now = time.time()
    enc_token = crypto.encrypt("refresh_token")

    conn = crud.create_connector(
        db_session,
        owner_id=test_user.id,
        type="email",
        provider="gmail",
        config={
            "refresh_token": enc_token,
            "history_id": 1,
            "watch_expiry": int((now - 60) * 1000),
        },
    )

    async def _exchange(_rt):
        return "access"

    watch_calls = {}

    def _start_watch(*, access_token, topic_name=None, callback_url=None, label_ids=None):
        watch_calls["topic_name"] = topic_name
        watch_calls["callback_url"] = callback_url
        return {
            "history_id": 55,
            "watch_expiry": int((now + 7 * 24 * 3600) * 1000),
        }

    monkeypatch.setattr(wrs.gmail_api, "async_exchange_refresh_token", _exchange)
    monkeypatch.setattr(wrs.gmail_api, "start_watch", _start_watch)
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: SimpleNamespace(app_public_url="https://app.test", gmail_pubsub_topic="projects/test/topics/gmail"),
    )

    service = wrs.WatchRenewalService()
    await service._process_connector_renewal(db_session, conn, now, now + 24 * 3600)

    updated = crud.get_connector(db_session, conn.id)
    assert updated is not None
    assert updated.config["history_id"] == 55
    assert updated.config["watch_expiry"] == int((now + 7 * 24 * 3600) * 1000)
    assert watch_calls["topic_name"] == "projects/test/topics/gmail"
    assert watch_calls["callback_url"] is None
