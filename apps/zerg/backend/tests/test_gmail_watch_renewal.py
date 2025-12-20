"""Test renewal stub wiring at connector level.

We set a connector's ``watch_expiry`` to the past and then call the provider's
``process_connector``. Renewal currently happens best-effort; this test only
validates that processing does not crash and can update connector history.
"""

from __future__ import annotations

import time

import pytest

from zerg.email.providers import GmailProvider


@pytest.mark.asyncio
async def test_gmail_process_connector_updates_history(client, db_session, _dev_user, monkeypatch):
    # Prepare connector with expired watch and baseline history
    from zerg.crud import crud as _crud

    # Encrypt dummy token
    from zerg.utils import crypto

    enc_token = crypto.encrypt("dummy_refresh_token")

    conn = _crud.create_connector(
        db_session,
        owner_id=_dev_user.id,
        type="email",
        provider="gmail",
        config={"refresh_token": enc_token, "history_id": 0, "watch_expiry": int(time.time() * 1000) - 1000},
    )

    # Patch token refresh and history
    from zerg.services import gmail_api as gmail_api_mod

    monkeypatch.setattr(gmail_api_mod, "exchange_refresh_token", lambda _rt: "access")

    async def _alist(_a, _h):
        return [{"id": "5", "messagesAdded": []}]

    monkeypatch.setattr(gmail_api_mod, "async_list_history", _alist)

    # Process
    prov = GmailProvider()
    await prov.process_connector(conn.id)

    # Assert history advanced on connector
    from zerg.database import default_session_factory
    from zerg.models.models import Connector as ConnectorModel

    with default_session_factory() as fresh:
        refreshed = fresh.query(ConnectorModel).filter(ConnectorModel.id == conn.id).first()
        assert refreshed is not None
        assert refreshed.config.get("history_id") == 5
