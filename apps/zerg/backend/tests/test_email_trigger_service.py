"""Basic smoke test for connector-centric email triggers.

Creates a Gmail connector, then an email trigger tied to it and verifies
the API returns the expected config (including provider and connector id).
"""

from __future__ import annotations


def test_create_email_trigger_with_connector(client, db_session, _dev_user):
    # 1) Create a Gmail connector
    from zerg.crud import crud as _crud

    conn = _crud.create_connector(
        db_session,
        owner_id=_dev_user.id,
        type="email",
        provider="gmail",
        config={"refresh_token": "enc:dummy"},
    )

    # 2) Create an fiche
    fiche_payload = {
        "name": "Email Trigger Fiche",
        "system_instructions": "sys",
        "task_instructions": "task",
        "model": "gpt-mock",
    }
    fiche_id = client.post("/api/fiches/", json=fiche_payload).json()["id"]

    # 3) Create an email trigger referencing the connector
    trigger_payload = {
        "fiche_id": fiche_id,
        "type": "email",
        "config": {"connector_id": conn.id},
    }
    resp = client.post("/api/triggers/", json=trigger_payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["type"] == "email"
    assert data["config"]["connector_id"] == conn.id
    assert data["config"]["provider"] == "gmail"
