"""Tests for runner WebSocket endpoint.

Tests the runner WebSocket connection lifecycle, authentication, and message handling.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.models.models import Runner, User
from zerg.services.runner_connection_manager import get_runner_connection_manager


@pytest.fixture
def test_runner(db: Session, test_user: User) -> tuple[Runner, str]:
    """Create a test runner with auth secret.

    Returns:
        Tuple of (runner, plaintext_secret)
    """
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
        labels={"env": "test"},
        capabilities=["exec.readonly"],
        metadata={"hostname": "test-host"},
    )
    return runner, secret


class TestRunnerWebSocket:
    """Tests for runner WebSocket endpoint."""

    def test_valid_connection(self, client: TestClient, db: Session, test_runner: tuple[Runner, str]):
        """Test successful WebSocket connection with valid credentials."""
        runner, secret = test_runner

        # Connect to WebSocket
        with client.websocket_connect("/api/runners/ws") as websocket:
            # Send hello message
            hello_msg = {
                "type": "hello",
                "runner_id": runner.id,
                "secret": secret,
                "metadata": {"hostname": "test-host", "platform": "linux"},
            }
            websocket.send_json(hello_msg)

            # Wait a moment for processing
            import time
            time.sleep(0.1)

            # Check runner status in database
            db.refresh(runner)
            assert runner.status == "online"
            assert runner.last_seen_at is not None
            assert runner.runner_metadata.get("hostname") == "test-host"

            # Check connection manager
            conn_manager = get_runner_connection_manager()
            assert conn_manager.is_online(runner.owner_id, runner.id)

    def test_invalid_runner_id(self, client: TestClient):
        """Test connection rejection with invalid runner_id."""
        with client.websocket_connect("/api/runners/ws") as websocket:
            hello_msg = {
                "type": "hello",
                "runner_id": 99999,
                "secret": "invalid-secret",
                "metadata": {},
            }
            websocket.send_json(hello_msg)

            # Connection should be closed with error
            with pytest.raises(Exception):
                websocket.receive_json()

    def test_invalid_secret(self, client: TestClient, test_runner: tuple[Runner, str]):
        """Test connection rejection with invalid secret."""
        runner, _ = test_runner

        with client.websocket_connect("/api/runners/ws") as websocket:
            hello_msg = {
                "type": "hello",
                "runner_id": runner.id,
                "secret": "wrong-secret",
                "metadata": {},
            }
            websocket.send_json(hello_msg)

            # Connection should be closed with error
            with pytest.raises(Exception):
                websocket.receive_json()

    def test_revoked_runner(self, client: TestClient, db: Session, test_runner: tuple[Runner, str]):
        """Test connection rejection for revoked runner."""
        runner, secret = test_runner

        # Revoke the runner
        runner_crud.revoke_runner(db, runner.id)

        with client.websocket_connect("/api/runners/ws") as websocket:
            hello_msg = {
                "type": "hello",
                "runner_id": runner.id,
                "secret": secret,
                "metadata": {},
            }
            websocket.send_json(hello_msg)

            # Connection should be closed with error
            with pytest.raises(Exception):
                websocket.receive_json()

    def test_heartbeat_updates_last_seen(
        self, client: TestClient, db: Session, test_runner: tuple[Runner, str]
    ):
        """Test that heartbeat messages update last_seen_at."""
        runner, secret = test_runner

        with client.websocket_connect("/api/runners/ws") as websocket:
            # Send hello
            hello_msg = {
                "type": "hello",
                "runner_id": runner.id,
                "secret": secret,
                "metadata": {},
            }
            websocket.send_json(hello_msg)

            import time
            time.sleep(0.1)

            # Get initial last_seen
            db.refresh(runner)
            initial_last_seen = runner.last_seen_at

            # Wait a bit
            time.sleep(0.2)

            # Send heartbeat
            heartbeat_msg = {"type": "heartbeat"}
            websocket.send_json(heartbeat_msg)

            time.sleep(0.1)

            # Check that last_seen was updated
            db.refresh(runner)
            assert runner.last_seen_at > initial_last_seen

    def test_disconnect_marks_offline(
        self, client: TestClient, db: Session, test_runner: tuple[Runner, str]
    ):
        """Test that disconnection marks runner as offline."""
        runner, secret = test_runner

        with client.websocket_connect("/api/runners/ws") as websocket:
            # Send hello
            hello_msg = {
                "type": "hello",
                "runner_id": runner.id,
                "secret": secret,
                "metadata": {},
            }
            websocket.send_json(hello_msg)

            import time
            time.sleep(0.1)

            # Verify online
            db.refresh(runner)
            assert runner.status == "online"

        # WebSocket closed, wait for cleanup
        import time
        time.sleep(0.2)

        # Check runner is offline
        db.refresh(runner)
        assert runner.status == "offline"

        # Check connection manager
        conn_manager = get_runner_connection_manager()
        assert not conn_manager.is_online(runner.owner_id, runner.id)

    def test_missing_hello_fields(self, client: TestClient):
        """Test connection rejection when hello message is missing fields."""
        with client.websocket_connect("/api/runners/ws") as websocket:
            # Missing secret
            hello_msg = {
                "type": "hello",
                "runner_id": 123,
                "metadata": {},
            }
            websocket.send_json(hello_msg)

            # Connection should be closed
            with pytest.raises(Exception):
                websocket.receive_json()

    def test_wrong_message_type(self, client: TestClient):
        """Test connection rejection when first message is not hello."""
        with client.websocket_connect("/api/runners/ws") as websocket:
            # Send heartbeat instead of hello
            heartbeat_msg = {"type": "heartbeat"}
            websocket.send_json(heartbeat_msg)

            # Connection should be closed
            with pytest.raises(Exception):
                websocket.receive_json()

    def test_replace_existing_connection(
        self, client: TestClient, db: Session, test_runner: tuple[Runner, str]
    ):
        """Test that a new connection replaces an existing one for the same runner."""
        runner, secret = test_runner

        # First connection
        with client.websocket_connect("/api/runners/ws") as websocket1:
            hello_msg = {
                "type": "hello",
                "runner_id": runner.id,
                "secret": secret,
                "metadata": {},
            }
            websocket1.send_json(hello_msg)

            import time
            time.sleep(0.1)

            # Verify online
            conn_manager = get_runner_connection_manager()
            assert conn_manager.is_online(runner.owner_id, runner.id)

            # Second connection (should replace first)
            with client.websocket_connect("/api/runners/ws") as websocket2:
                websocket2.send_json(hello_msg)
                time.sleep(0.1)

                # Should still be online with new connection
                assert conn_manager.is_online(runner.owner_id, runner.id)

        # All connections closed
        import time
        time.sleep(0.2)

        # Should be offline now
        assert not conn_manager.is_online(runner.owner_id, runner.id)


class TestConnectionManager:
    """Tests for RunnerConnectionManager."""

    def test_register_and_get_connection(self):
        """Test registering and retrieving connections."""
        from unittest.mock import Mock

        manager = get_runner_connection_manager()
        mock_ws = Mock()

        # Register
        manager.register(1, 100, mock_ws)

        # Get connection
        ws = manager.get_connection(1, 100)
        assert ws == mock_ws

        # Check online
        assert manager.is_online(1, 100)

        # Unregister
        manager.unregister(1, 100)
        assert not manager.is_online(1, 100)
        assert manager.get_connection(1, 100) is None

    def test_get_online_count(self):
        """Test counting online runners."""
        from unittest.mock import Mock

        manager = get_runner_connection_manager()

        # Clear any existing connections
        manager._connections.clear()

        # Add some connections
        manager.register(1, 100, Mock())
        manager.register(1, 101, Mock())
        manager.register(2, 200, Mock())

        # Count all
        assert manager.get_online_count() == 3

        # Count by owner
        assert manager.get_online_count(owner_id=1) == 2
        assert manager.get_online_count(owner_id=2) == 1
        assert manager.get_online_count(owner_id=3) == 0

        # Cleanup
        manager._connections.clear()
