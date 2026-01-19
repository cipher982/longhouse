"""Tests for Gmail observability metrics."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_gmail_connector_history_metric_updates(db_session, test_user, monkeypatch):
    """Processing a connector should update the history_id gauge."""

    from zerg.crud import crud
    from zerg.email.providers import GmailProvider
    from zerg.utils import crypto
    import zerg.metrics as metrics_mod
    import zerg.services.gmail_api as gmail_api_mod

    conn = crud.create_connector(
        db_session,
        owner_id=test_user.id,
        type="email",
        provider="gmail",
        config={"refresh_token": crypto.encrypt("rt"), "history_id": 0},
    )

    async def _exchange(_rt):
        return "access"

    async def _list_history(_access, _start):
        return [{"id": "10", "messagesAdded": [{"message": {"id": "m1"}}]}]

    async def _get_meta(_access, _msg_id):
        return {}

    monkeypatch.setattr(gmail_api_mod, "async_exchange_refresh_token", _exchange)
    monkeypatch.setattr(gmail_api_mod, "async_list_history", _list_history)
    monkeypatch.setattr(gmail_api_mod, "async_get_message_metadata", _get_meta)

    mock_gauge = MagicMock()
    monkeypatch.setattr(metrics_mod, "gmail_connector_history_id", mock_gauge)

    provider = GmailProvider()
    await provider.process_connector(conn.id)

    mock_gauge.labels.assert_called_with(connector_id=str(conn.id), owner_id=str(conn.owner_id))
    mock_gauge.labels.return_value.set.assert_called_with(10)
