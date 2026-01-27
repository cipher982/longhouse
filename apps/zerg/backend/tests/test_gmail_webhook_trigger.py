"""End-to-end smoke-test for Gmail webhook → trigger → fiche execution.

The scenario:

1. Create an *fiche* and an associated *email* trigger referencing a Gmail connector.
2. Connect Gmail to store a refresh token (monkey-patched).
3. Fire the Gmail webhook callback with connector id in channel token.
"""

from __future__ import annotations

import asyncio

import pytest

from zerg.routers import auth as auth_router
from zerg.services import gmail_api as gmail_api_mod
from zerg.services.scheduler_service import scheduler_service

# ---------------------------------------------------------------------------
# Helper fixtures / monkey-patching
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_google_token_exchange(monkeypatch):
    """Replace the network call in `_exchange_google_auth_code` with stub."""

    fake_tokens = {"refresh_token": "test-refresh-token", "access_token": "ignore"}

    monkeypatch.setattr(auth_router, "_exchange_google_auth_code", lambda _code: fake_tokens)


# ---------------------------------------------------------------------------
# Main test coroutine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_webhook_triggers_agent(client, db_session, _dev_user):
    """Full flow: trigger creation → Gmail connect → webhook callback."""

    # 1) Create fiche ----------------------------------------------------
    fiche_payload = {
        "name": "Gmail Fiche",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": "gpt-mock",
    }

    resp = client.post("/api/fiches/", json=fiche_payload)
    assert resp.status_code == 201, resp.text
    fiche_id = resp.json()["id"]

    # 2) Create Gmail connector + email trigger ------------------------
    from zerg.crud import crud as _crud

    # Check if connector already exists (from previous test runs)
    existing = _crud.get_connectors(db_session, owner_id=_dev_user.id, type="email", provider="gmail")
    if existing:
        conn = existing[0]
        # Update config to ensure clean state
        _crud.update_connector(db_session, conn.id, config={"refresh_token": "enc", "history_id": 0})
    else:
        conn = _crud.create_connector(
            db_session,
            owner_id=_dev_user.id,
            type="email",
            provider="gmail",
            config={"refresh_token": "enc", "history_id": 0},
        )
    trg_resp = client.post(
        "/api/triggers/",
        json={"fiche_id": fiche_id, "type": "email", "config": {"connector_id": conn.id}},
    )
    assert trg_resp.status_code == 201, trg_resp.text

    # 3) Connect Gmail (endpoint stubbed – no-op for connector already present)
    connect_resp = client.post("/api/auth/google/gmail", json={"auth_code": "dummy"})
    assert connect_resp.status_code == 200, connect_resp.text

    # 4) Monkey-patch Gmail helpers + scheduler so diff logic sees one message

    def _stub_list_history(_access: str, _start_id: int):  # noqa: D401 – stub sync
        return [{"id": "1001", "messagesAdded": [{"message": {"id": "mid-1"}}]}]

    def _stub_get_meta(_access: str, _mid: str):  # noqa: D401 – stub sync
        return {
            "id": _mid,
            "labelIds": ["INBOX"],
            "headers": {"From": "alice@example.com", "Subject": "Hello"},
        }

    monkeypatch = pytest.MonkeyPatch()
    # ------------------------------------------------------------------
    # Patch *token refresh* so the handler does not require real env vars
    # ------------------------------------------------------------------
    monkeypatch.setattr(gmail_api_mod, "exchange_refresh_token", lambda _rt: "access-token-dummy")
    monkeypatch.setattr(gmail_api_mod, "list_history", _stub_list_history)
    monkeypatch.setattr(gmail_api_mod, "get_message_metadata", _stub_get_meta)

    called = {"count": 0, "fiche_id": None}

    async def _stub_run_fiche_task(aid: int, trigger: str = "schedule"):  # noqa: D401 – stub async
        called["count"] += 1
        called["fiche_id"] = aid

    original_run = scheduler_service.run_fiche_task
    scheduler_service.run_fiche_task = _stub_run_fiche_task  # type: ignore[assignment]

    try:
        # 5) Fire Gmail webhook (msg_no=1) -----------------------------
        headers = {"X-Goog-Channel-Token": str(conn.id), "X-Goog-Message-Number": "1"}
        wh_resp = client.post("/api/email/webhook/google", headers=headers)
        assert wh_resp.status_code == 202, wh_resp.text

        # Allow event-loop tasks time to run (webhook is async now)
        await asyncio.sleep(0.2)

        assert called["count"] == 1
        assert called["fiche_id"] == fiche_id

        # Inspect stored last_msg_no before second callback
        from zerg.models.models import Connector as ConnectorModel

        stored = db_session.query(ConnectorModel).filter(ConnectorModel.id == conn.id).first()
        assert stored is not None
        assert stored.config.get("last_msg_no") == 1

        # 6) Fire webhook again with SAME message number → should dedup
        wh_resp2 = client.post("/api/email/webhook/google", headers=headers)
        assert wh_resp2.status_code == 202, wh_resp2.text

        await asyncio.sleep(0)
        assert called["count"] == 1, "Second webhook with same msg_no should not trigger run"
    finally:
        scheduler_service.run_fiche_task = original_run  # type: ignore[assignment]
        monkeypatch.undo()
