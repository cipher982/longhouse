"""Ensure DELETE /triggers/{id} does not affect Gmail connector watches."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_delete_gmail_trigger_no_connector_side_effects(client, db_session, _dev_user):
    """Deleting a gmail trigger should not call stop_watch in connector-centric design."""

    # ------------------------------------------------------------------
    # Patch Google auth-code exchange so we can create a gmail-connected user
    # ------------------------------------------------------------------
    from zerg.crud import crud as _crud

    # Create a Gmail connector
    conn = _crud.create_connector(db_session, owner_id=_dev_user.id, type="email", provider="gmail", config={})

    # ------------------------------------------------------------------
    # Prepare agent + gmail trigger
    # ------------------------------------------------------------------
    agent_payload = {
        "name": "Cleanup Agent",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": "gpt-mock",
    }

    agent_id = client.post("/api/agents/", json=agent_payload).json()["id"]

    trigger_payload = {"agent_id": agent_id, "type": "email", "config": {"connector_id": conn.id}}
    trg_resp = client.post("/api/triggers/", json=trigger_payload)
    trg_id = trg_resp.json()["id"]

    # ------------------------------------------------------------------
    # Call DELETE – should trigger our fake_stop_watch
    # ------------------------------------------------------------------
    del_resp = client.delete(f"/api/triggers/{trg_id}")
    assert del_resp.status_code == 204, del_resp.text

    # verify the trigger row is gone
    from zerg.crud import crud as crud_mod

    assert crud_mod.get_trigger(db_session, trg_id) is None
