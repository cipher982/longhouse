"""Regression tests for Gmail webhook → connector-centric integration.

Focus on *observable* behaviour rather than internal helper functions:

1. After a webhook callback the connector's ``history_id`` must advance to the
   highest ``history.id`` value returned by the Gmail *history* diff.
2. A *new* ``X-Goog-Message-Number`` should schedule an additional course
   (dedup logic only skips identical numbers).

These tests reuse the existing helper stubs from ``test_gmail_webhook_trigger``
but patch *list_history* dynamically to simulate multiple pushes.
"""

from __future__ import annotations

import asyncio

import pytest

from zerg.services.scheduler_service import scheduler_service

# ---------------------------------------------------------------------------
# Shared helpers / monkey-patches
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_refresh_token(monkeypatch):
    """Patch *token refresh* endpoint so the service is environment-agnostic."""

    from zerg.services import gmail_api as gmail_api_mod  # noqa: WPS433

    monkeypatch.setattr(gmail_api_mod, "exchange_refresh_token", lambda _rt: "dummy-access")


# ---------------------------------------------------------------------------
# Main test coroutine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_id_advancement_and_dedup(client, db_session, _dev_user, monkeypatch):
    """Webhook should update history_id and fire again on *new* message numbers."""

    # --------------------------------- 1) Connect Gmail (store refresh token)
    from zerg.routers import auth as auth_router

    monkeypatch.setattr(
        auth_router,
        "_exchange_google_auth_code",
        lambda _code: {"refresh_token": "rt", "access_token": "at"},
    )

    resp = client.post("/api/auth/google/gmail", json={"auth_code": "x"})
    connector_id = resp.json().get("connector_id")

    # --------------------------------- 2) Update the connector's history_id to 0
    from zerg.crud import crud as _crud

    conn = _crud.get_connector(db_session, connector_id)
    config = dict(conn.config or {})
    config["history_id"] = 0
    _crud.update_connector(db_session, conn.id, config=config)

    # --------------------------------- 3) Prepare fiche + gmail trigger
    fiche_id = client.post(
        "/api/fiches/",
        json={
            "name": "Gmail Progress Fiche",
            "system_instructions": "sys",
            "task_instructions": "task",
            "model": "gpt-mock",
        },
    ).json()["id"]

    client.post(
        "/api/triggers/",
        json={"fiche_id": fiche_id, "type": "email", "config": {"connector_id": conn.id}},
    )

    # --------------------------------- 4) Stub list_history with *stateful* behaviour

    from zerg.services import gmail_api as gmail_api_mod  # noqa: WPS433 – re-import for monkeypatch

    history_calls = {"count": 0}

    def _list_history(_access: str, _start_id: int):  # noqa: D401 – sync helper
        """Return different history payloads on successive invocations."""

        history_calls["count"] += 1
        if history_calls["count"] == 1:
            return [{"id": "1001", "messagesAdded": [{"message": {"id": "mid-1"}}]}]
        # second invocation – new history id + new message
        return [{"id": "1002", "messagesAdded": [{"message": {"id": "mid-2"}}]}]

    monkeypatch.setattr(gmail_api_mod, "list_history", _list_history)

    # Minimal metadata stub (always matches filters)
    monkeypatch.setattr(
        gmail_api_mod,
        "get_message_metadata",
        lambda _a, _m: {"id": _m, "labelIds": ["INBOX"], "headers": {"From": "a", "Subject": "b"}},
    )

    # --------------------------------- 5) Count fiche executions
    exec_counter = {"runs": 0}

    async def _stub_run(aid: int, trigger: str = "schedule"):  # noqa: D401 – async stub
        exec_counter["runs"] += 1
        exec_counter["last_aid"] = aid

    original_runner = scheduler_service.run_fiche_task
    scheduler_service.run_fiche_task = _stub_run  # type: ignore[assignment]

    try:
        # -------------------------- first webhook (msg_no=1) – should run
        client.post(
            "/api/email/webhook/google",
            headers={"X-Goog-Channel-Token": str(conn.id), "X-Goog-Message-Number": "1"},
        )

        await asyncio.sleep(0.1)  # Give async task time to run

        assert exec_counter["runs"] == 1

        # history_id must be 1001 now
        from zerg.database import default_session_factory
        from zerg.models.models import Connector as ConnectorModel

        with default_session_factory() as fresh:
            conn_after = fresh.query(ConnectorModel).filter(ConnectorModel.id == conn.id).first()
            assert conn_after is not None
            assert conn_after.config.get("history_id") == 1001

        # -------------------------- second webhook (msg_no=2) – should run again
        client.post(
            "/api/email/webhook/google",
            headers={"X-Goog-Channel-Token": str(conn.id), "X-Goog-Message-Number": "2"},
        )

        await asyncio.sleep(0.1)  # Give async task time to run

        assert exec_counter["runs"] == 2, "New message number should schedule another run"

        # history_id advanced again
        with default_session_factory() as fresh2:
            conn_final = fresh2.query(ConnectorModel).filter(ConnectorModel.id == conn.id).first()
            assert conn_final is not None
            assert conn_final.config.get("history_id") == 1002

    finally:
        scheduler_service.run_fiche_task = original_runner  # type: ignore[assignment]
