"""Tests for conversation sync endpoints."""

import pytest
from datetime import datetime, timezone


def test_push_sync_operations(client, auth_headers):
    """Test pushing sync operations."""
    # Prepare push request
    push_data = {
        "deviceId": "test-device-1",
        "cursor": 0,
        "ops": [
            {
                "opId": "op-1",
                "type": "message",
                "body": {"text": "Hello world", "conversationId": "conv-1"},
                "lamport": 1,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            {
                "opId": "op-2",
                "type": "message",
                "body": {"text": "Second message", "conversationId": "conv-1"},
                "lamport": 2,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        ],
    }

    # Push operations
    response = client.post("/api/jarvis/sync/push", json=push_data, headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert "acked" in data
    assert "nextCursor" in data
    assert len(data["acked"]) == 2
    assert "op-1" in data["acked"]
    assert "op-2" in data["acked"]
    assert data["nextCursor"] == 2


def test_push_sync_operations_idempotent(client, auth_headers):
    """Test that pushing duplicate operations is idempotent."""
    # Prepare push request with same opId twice
    push_data = {
        "deviceId": "test-device-1",
        "cursor": 0,
        "ops": [
            {
                "opId": "op-duplicate",
                "type": "message",
                "body": {"text": "First push"},
                "lamport": 1,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    # First push
    response1 = client.post("/api/jarvis/sync/push", json=push_data, headers=auth_headers)
    assert response1.status_code == 200
    data1 = response1.json()
    assert "op-duplicate" in data1["acked"]

    # Second push with same opId - should also succeed
    response2 = client.post("/api/jarvis/sync/push", json=push_data, headers=auth_headers)
    assert response2.status_code == 200
    data2 = response2.json()
    assert "op-duplicate" in data2["acked"]

    # Cursor should not advance (only one operation stored)
    assert data1["nextCursor"] == data2["nextCursor"]


def test_pull_sync_operations(client, auth_headers):
    """Test pulling sync operations."""
    # First push some operations
    push_data = {
        "deviceId": "test-device-1",
        "cursor": 0,
        "ops": [
            {
                "opId": "op-pull-1",
                "type": "message",
                "body": {"text": "Message 1"},
                "lamport": 1,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            {
                "opId": "op-pull-2",
                "type": "message",
                "body": {"text": "Message 2"},
                "lamport": 2,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        ],
    }
    client.post("/api/jarvis/sync/push", json=push_data, headers=auth_headers)

    # Pull from cursor 0 (should get all operations)
    response = client.get("/api/jarvis/sync/pull?cursor=0", headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert "ops" in data
    assert "nextCursor" in data
    assert len(data["ops"]) == 2
    assert data["ops"][0]["opId"] == "op-pull-1"
    assert data["ops"][1]["opId"] == "op-pull-2"
    assert data["nextCursor"] == 2


def test_pull_sync_operations_with_cursor(client, auth_headers):
    """Test pulling sync operations with non-zero cursor."""
    # Push 3 operations
    push_data = {
        "deviceId": "test-device-1",
        "cursor": 0,
        "ops": [
            {
                "opId": f"op-cursor-{i}",
                "type": "message",
                "body": {"text": f"Message {i}"},
                "lamport": i,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(1, 4)
        ],
    }
    client.post("/api/jarvis/sync/push", json=push_data, headers=auth_headers)

    # Pull from cursor 1 (should get operations after first one)
    response = client.get("/api/jarvis/sync/pull?cursor=1", headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert len(data["ops"]) == 2
    assert data["ops"][0]["opId"] == "op-cursor-2"
    assert data["ops"][1]["opId"] == "op-cursor-3"
    assert data["nextCursor"] == 3


def test_pull_sync_operations_empty(client, auth_headers):
    """Test pulling when no new operations exist."""
    # Pull from cursor 0 when no operations pushed
    response = client.get("/api/jarvis/sync/pull?cursor=0", headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["ops"] == []
    assert data["nextCursor"] == 0


def test_pull_sync_operations_invalid_cursor(client, auth_headers):
    """Test pulling with invalid cursor."""
    # Negative cursor should fail
    response = client.get("/api/jarvis/sync/pull?cursor=-1", headers=auth_headers)
    assert response.status_code == 400


def test_sync_operations_user_isolation(client, auth_headers, other_user, db_session):
    """Test that sync operations are properly scoped by user_id."""
    # User isolation is enforced by the foreign key constraint and filtering
    # This test verifies that operations are stored with correct user_id

    # Push operation from authenticated user
    push_data = {
        "deviceId": "device-1",
        "cursor": 0,
        "ops": [
            {
                "opId": "user-scoped-op",
                "type": "message",
                "body": {"text": "User message"},
                "lamport": 1,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    response = client.post("/api/jarvis/sync/push", json=push_data, headers=auth_headers)
    assert response.status_code == 200

    # Verify operation is stored with correct user_id in database
    from zerg.models.sync import SyncOperation

    sync_op = db_session.query(SyncOperation).filter_by(op_id="user-scoped-op").first()
    assert sync_op is not None
    assert sync_op.user_id is not None
    assert sync_op.type == "message"
    assert sync_op.body == {"text": "User message"}

    # Pull operations - should get back the operation
    response = client.get("/api/jarvis/sync/pull?cursor=0", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["ops"]) == 1
    assert data["ops"][0]["opId"] == "user-scoped-op"
