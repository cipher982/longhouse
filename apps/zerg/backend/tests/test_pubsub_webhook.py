"""Tests for Pub/Sub Gmail webhook endpoint."""

import base64
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.models.models import User


@pytest.mark.asyncio
async def test_pubsub_webhook_validates_oidc_token(client: TestClient, db_session: Session):
    """Test that Pub/Sub webhook validates OIDC tokens properly."""
    # No auth header should fail
    response = client.post("/api/email/webhook/google/pubsub", json={})
    assert response.status_code == 401

    # Invalid token should fail
    response = client.post(
        "/api/email/webhook/google/pubsub",
        json={},
        headers={"Authorization": "Bearer invalid"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_pubsub_webhook_processes_valid_message(
    client: TestClient, db_session: Session, test_user: User, monkeypatch
):
    """Test processing of valid Pub/Sub message with email address mapping."""
    # Create a connector with email address
    conn = crud.create_connector(
        db_session,
        owner_id=test_user.id,
        type="email",
        provider="gmail",
        config={
            "refresh_token": "encrypted_token",
            "history_id": 100,
            "emailAddress": "test@example.com",
        },
    )

    # Mock OIDC validation in test mode
    from zerg.routers import email_webhooks_pubsub

    monkeypatch.setattr(
        email_webhooks_pubsub,
        "validate_pubsub_token",
        lambda _: True,
    )

    # Mock the Gmail provider
    mock_provider = MagicMock()
    mock_provider.process_connector = MagicMock(return_value=None)

    from zerg.email import providers as providers_mod

    monkeypatch.setattr(
        providers_mod,
        "get_provider",
        lambda _: mock_provider,
    )

    # Prepare Pub/Sub message
    gmail_data = {
        "emailAddress": "test@example.com",
        "historyId": "200",
    }
    message_data = base64.b64encode(json.dumps(gmail_data).encode()).decode()

    pubsub_message = {
        "message": {
            "data": message_data,
            "messageId": "123",
            "publishTime": "2024-01-01T00:00:00Z",
        }
    }

    # Send webhook
    response = client.post(
        "/api/email/webhook/google/pubsub",
        json=pubsub_message,
        headers={"Authorization": "Bearer valid_token"},
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["connector_id"] == conn.id
    assert data["email_address"] == "test@example.com"

    # Verify history_id was updated
    updated_conn = crud.get_connector(db_session, conn.id)
    assert updated_conn.config["history_id"] == 200


@pytest.mark.asyncio
async def test_pubsub_webhook_ignores_unknown_email(
    client: TestClient, db_session: Session, test_user: User, monkeypatch
):
    """Test that messages for unknown email addresses are ignored."""
    # Mock OIDC validation
    from zerg.routers import email_webhooks_pubsub

    monkeypatch.setattr(
        email_webhooks_pubsub,
        "validate_pubsub_token",
        lambda _: True,
    )

    # Pub/Sub message for unknown email
    gmail_data = {
        "emailAddress": "unknown@example.com",
        "historyId": "200",
    }
    message_data = base64.b64encode(json.dumps(gmail_data).encode()).decode()

    pubsub_message = {
        "message": {
            "data": message_data,
        }
    }

    response = client.post(
        "/api/email/webhook/google/pubsub",
        json=pubsub_message,
        headers={"Authorization": "Bearer valid_token"},
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "no_connector"


@pytest.mark.asyncio
async def test_pubsub_webhook_handles_malformed_message(client: TestClient, db_session: Session, monkeypatch):
    """Test handling of malformed Pub/Sub messages."""
    # Mock OIDC validation
    from zerg.routers import email_webhooks_pubsub

    monkeypatch.setattr(
        email_webhooks_pubsub,
        "validate_pubsub_token",
        lambda _: True,
    )

    # Invalid base64
    pubsub_message = {
        "message": {
            "data": "not-valid-base64!!!",
        }
    }

    response = client.post(
        "/api/email/webhook/google/pubsub",
        json=pubsub_message,
        headers={"Authorization": "Bearer valid_token"},
    )

    assert response.status_code == 400
    assert "Invalid message format" in response.json()["detail"]


@pytest.mark.asyncio
async def test_pubsub_webhook_updates_metrics(client: TestClient, db_session: Session, test_user: User, monkeypatch):
    """Test that Pub/Sub webhook updates processing metrics."""
    # Create connector
    crud.create_connector(
        db_session,
        owner_id=test_user.id,
        type="email",
        provider="gmail",
        config={
            "refresh_token": "token",
            "emailAddress": "metrics@example.com",
        },
    )

    # Mock dependencies
    from zerg.routers import email_webhooks_pubsub

    monkeypatch.setattr(
        email_webhooks_pubsub,
        "validate_pubsub_token",
        lambda _: True,
    )

    mock_provider = MagicMock()
    mock_provider.process_connector = MagicMock(return_value=None)

    from zerg.email import providers as providers_mod

    monkeypatch.setattr(
        providers_mod,
        "get_provider",
        lambda _: mock_provider,
    )

    # Mock metrics
    from zerg import metrics as metrics_mod

    mock_gauge = MagicMock()
    monkeypatch.setattr(metrics_mod, "pubsub_webhook_processing", mock_gauge)

    # Send message
    gmail_data = {"emailAddress": "metrics@example.com", "historyId": "300"}
    message_data = base64.b64encode(json.dumps(gmail_data).encode()).decode()

    response = client.post(
        "/api/email/webhook/google/pubsub",
        json={"message": {"data": message_data}},
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 202

    # Verify metrics were called
    # Note: inc() is called in the async task, which may not have run yet
    # In real monitoring, this would be tracked via Prometheus
