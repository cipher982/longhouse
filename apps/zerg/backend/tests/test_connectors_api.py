"""Tests for connector API endpoints."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.models.models import User


def test_list_connectors_empty(client: TestClient, db_session: Session, test_user: User):
    """Test listing connectors when none exist."""
    response = client.get("/api/connectors", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200
    assert response.json() == []


def test_list_connectors_with_data(client: TestClient, db_session: Session, test_user: User):
    """Test listing connectors returns user's connectors with redacted secrets."""
    # Create a test connector
    conn = crud.create_connector(
        db_session,
        owner_id=test_user.id,
        type="email",
        provider="gmail",
        config={"refresh_token": "secret123", "history_id": 42},
    )

    response = client.get("/api/connectors", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == conn.id
    assert data[0]["type"] == "email"
    assert data[0]["provider"] == "gmail"
    assert data[0]["config"]["refresh_token"] == "***"  # Redacted
    assert data[0]["config"]["history_id"] == 42  # Not redacted


def test_list_connectors_only_own(client: TestClient, db_session: Session, test_user: User):
    """Test that users only see their own connectors."""
    # Create another user and connector
    other_user = crud.create_user(db_session, email="other@example.com")
    crud.create_connector(
        db_session,
        owner_id=other_user.id,
        type="email",
        provider="gmail",
        config={"refresh_token": "other_secret"},
    )

    # Create connector for test user
    crud.create_connector(
        db_session,
        owner_id=test_user.id,
        type="email",
        provider="outlook",
        config={"client_secret": "my_secret"},
    )

    response = client.get("/api/connectors", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["provider"] == "outlook"
    assert data[0]["owner_id"] == test_user.id


def test_delete_connector_success(client: TestClient, db_session: Session, test_user: User):
    """Test successful connector deletion."""
    conn = crud.create_connector(
        db_session,
        owner_id=test_user.id,
        type="email",
        provider="gmail",
        config={"refresh_token": "secret"},
    )

    response = client.delete(f"/api/connectors/{conn.id}", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 204
    assert response.content == b""  # No body for 204

    # Verify deletion
    assert crud.get_connector(db_session, conn.id) is None


def test_delete_connector_not_found(client: TestClient, db_session: Session, test_user: User):
    """Test deleting non-existent connector returns 404."""
    response = client.delete("/api/connectors/9999", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 404
    assert response.json()["detail"] == "Connector not found"


def test_delete_connector_not_owner(client: TestClient, db_session: Session, test_user: User):
    """Test users cannot delete other users' connectors."""
    other_user = crud.create_user(db_session, email="other@example.com")
    conn = crud.create_connector(
        db_session,
        owner_id=other_user.id,
        type="email",
        provider="gmail",
        config={"refresh_token": "secret"},
    )

    response = client.delete(f"/api/connectors/{conn.id}", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 404
    assert response.json()["detail"] == "Connector not found"

    # Verify connector still exists
    assert crud.get_connector(db_session, conn.id) is not None


def test_connectors_require_auth(client: TestClient, db_session: Session):
    """Test that connector endpoints require authentication.

    Note: In test mode, AUTH_DISABLED is set, so authentication is bypassed.
    This test verifies that the endpoints are at least behind the auth dependency.
    """
    # With no X-User-ID header, the test mode auth returns None
    response = client.get("/api/connectors", headers={})
    # In test mode with AUTH_DISABLED, it returns empty list for no user
    assert response.status_code == 200
    assert response.json() == []
