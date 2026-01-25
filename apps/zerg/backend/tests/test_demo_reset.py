"""Tests for demo account reset functionality.

Verifies that:
1. Demo user data is properly cleared across all tables
2. FK ordering is correct (no violations)
3. User account is preserved after reset
4. Non-demo users cannot be reset
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zerg.crud import crud
from zerg.dependencies import auth as auth_dep
from zerg.models.agent import Agent
from zerg.models.connector import Connector
from zerg.models.models import (
    AgentRun,
    CanvasLayout,
    Thread,
    ThreadMessage,
    User,
    Workflow,
    WorkflowExecution,
    WorkerJob,
)
from zerg.models.enums import RunStatus, ThreadType
from zerg.routers import auth as auth_router
from zerg.routers.admin import reset_demo_user_data


def _google_login(client: TestClient, monkeypatch, email: str) -> str:
    """Return JWT by stubbing Google verification."""
    fake_claims = {"email": email, "sub": "google-test"}
    monkeypatch.setattr(auth_router, "_verify_google_id_token", lambda _tok: fake_claims)
    resp = client.post("/api/auth/google", json={"id_token": "dummy"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _create_demo_user(db_session, email: str = "demo@test.local") -> User:
    """Create a demo user for testing."""
    user = User(
        email=email,
        provider="demo",
        provider_user_id=email,
        role="USER",
        is_active=True,
        display_name="Demo User",
        prefs={"demo": True},
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _create_user_data(db_session, user: User) -> dict:
    """Create various data owned by the user.

    Returns dict of created object counts for verification.
    """
    counts = {}

    # Create an agent
    agent = Agent(
        owner_id=user.id,
        name="Test Agent",
        system_instructions="Test system instructions",
        task_instructions="Test task instructions",
        model="gpt-test",
    )
    db_session.add(agent)
    db_session.flush()
    counts["agents"] = 1

    # Create a thread for the agent
    thread = Thread(
        agent_id=agent.id,
        title="Test Thread",
        active=False,
        thread_type=ThreadType.CHAT.value,
    )
    db_session.add(thread)
    db_session.flush()
    counts["threads"] = 1

    # Create thread messages
    msg = ThreadMessage(
        thread_id=thread.id,
        role="user",
        content="Test message",
    )
    db_session.add(msg)
    counts["thread_messages"] = 1

    # Create an agent run
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.SUCCESS,
    )
    db_session.add(run)
    db_session.flush()
    counts["agent_runs"] = 1

    # Create a workflow
    workflow = Workflow(
        owner_id=user.id,
        name="Test Workflow",
        canvas={},
    )
    db_session.add(workflow)
    db_session.flush()
    counts["workflows"] = 1

    # Create a canvas layout linked to the workflow (tests FK ordering)
    canvas = CanvasLayout(
        user_id=user.id,
        workflow_id=workflow.id,
        nodes_json={"nodes": [], "edges": []},
    )
    db_session.add(canvas)
    counts["canvas_layouts"] = 1

    # Create a connector
    connector = Connector(
        owner_id=user.id,
        type="test",
        provider="test_provider",
    )
    db_session.add(connector)
    counts["connectors"] = 1

    # Create a worker job
    worker_job = WorkerJob(
        owner_id=user.id,
        supervisor_run_id=run.id,
        task="Test task",
        status="completed",
        model="gpt-test",
    )
    db_session.add(worker_job)
    counts["worker_jobs"] = 1

    db_session.commit()
    return counts


class TestResetDemoUserData:
    """Tests for the reset_demo_user_data function."""

    def test_reset_clears_all_user_data(self, db_session):
        """Reset should clear all data owned by the demo user."""
        user = _create_demo_user(db_session)
        _create_user_data(db_session, user)

        # Verify data exists before reset
        assert db_session.query(Agent).filter(Agent.owner_id == user.id).count() == 1
        assert db_session.query(Workflow).filter(Workflow.owner_id == user.id).count() == 1
        assert db_session.query(CanvasLayout).filter(CanvasLayout.user_id == user.id).count() == 1

        # Reset the demo user
        cleared = reset_demo_user_data(db_session, user.id)

        # Verify all data is cleared
        assert db_session.query(Agent).filter(Agent.owner_id == user.id).count() == 0
        assert db_session.query(Workflow).filter(Workflow.owner_id == user.id).count() == 0
        assert db_session.query(CanvasLayout).filter(CanvasLayout.user_id == user.id).count() == 0
        assert db_session.query(Connector).filter(Connector.owner_id == user.id).count() == 0
        assert db_session.query(WorkerJob).filter(WorkerJob.owner_id == user.id).count() == 0

        # Verify counts returned
        assert cleared.get("agents") == 1
        assert cleared.get("workflows") == 1
        assert cleared.get("canvas_layouts") == 1

    def test_reset_preserves_user_account(self, db_session):
        """Reset should preserve the user account itself."""
        user = _create_demo_user(db_session)
        _create_user_data(db_session, user)

        reset_demo_user_data(db_session, user.id)

        # User should still exist
        db_session.expire_all()
        preserved_user = db_session.query(User).filter(User.id == user.id).first()
        assert preserved_user is not None
        assert preserved_user.email == user.email
        assert preserved_user.prefs.get("demo") is True

    def test_reset_handles_canvas_workflow_fk(self, db_session):
        """Reset should handle CanvasLayout -> Workflow FK correctly.

        This tests the specific FK ordering bug where CanvasLayout.workflow_id
        references Workflow.id. CanvasLayout must be deleted before Workflow.
        """
        user = _create_demo_user(db_session)

        # Create workflow first
        workflow = Workflow(
            owner_id=user.id,
            name="FK Test Workflow",
            canvas={},
        )
        db_session.add(workflow)
        db_session.flush()

        # Create canvas layout linked to workflow
        canvas = CanvasLayout(
            user_id=user.id,
            workflow_id=workflow.id,
            nodes_json={"test": True},
        )
        db_session.add(canvas)
        db_session.commit()

        # This should NOT raise FK violation
        cleared = reset_demo_user_data(db_session, user.id)

        # Both should be cleared
        assert cleared.get("canvas_layouts") == 1
        assert cleared.get("workflows") == 1

        # Verify they're actually gone
        assert db_session.query(CanvasLayout).filter(CanvasLayout.user_id == user.id).count() == 0
        assert db_session.query(Workflow).filter(Workflow.owner_id == user.id).count() == 0

    def test_reset_handles_empty_user(self, db_session):
        """Reset should handle a user with no data gracefully."""
        user = _create_demo_user(db_session)

        # Reset user with no data - should not raise
        cleared = reset_demo_user_data(db_session, user.id)

        # Counts should be empty or zero
        assert sum(cleared.values()) == 0


class TestDemoResetEndpoint:
    """Tests for the /admin/demo-users/{user_id}/reset endpoint."""

    @pytest.fixture(autouse=True)
    def _dev_env(self, monkeypatch):
        """Force development environment."""
        monkeypatch.setenv("ENVIRONMENT", "development")

    def test_reset_requires_super_admin(self, monkeypatch, client: TestClient, db_session):
        """Only super admins can reset demo users."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        # Create demo user
        demo_user = _create_demo_user(db_session, "demo-target@test.local")

        # Login as non-admin
        token = _google_login(client, monkeypatch, "regular@test.local")

        resp = client.post(
            f"/api/admin/demo-users/{demo_user.id}/reset",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 403

    def test_reset_rejects_non_demo_user(self, monkeypatch, client: TestClient, db_session):
        """Cannot reset a non-demo user."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)
        monkeypatch.setenv("ADMIN_EMAILS", "admin@test.local")

        # Create a non-demo user
        target_user = User(
            email="notdemo@test.local",
            provider="google",
            provider_user_id="notdemo",
            role="USER",
            is_active=True,
            prefs={},  # No demo flag
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        # Login as super admin
        token = _google_login(client, monkeypatch, "admin@test.local")
        admin = crud.get_user_by_email(db_session, "admin@test.local")
        admin.role = "ADMIN"
        db_session.commit()

        resp = client.post(
            f"/api/admin/demo-users/{target_user.id}/reset",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 403
        assert "demo" in resp.json()["detail"].lower()

    def test_reset_endpoint_clears_demo_data(self, monkeypatch, client: TestClient, db_session):
        """Endpoint should clear demo user data and return counts."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)
        monkeypatch.setenv("ADMIN_EMAILS", "admin@test.local")

        # Create demo user with data
        demo_user = _create_demo_user(db_session, "demo-with-data@test.local")
        _create_user_data(db_session, demo_user)

        # Login as super admin
        token = _google_login(client, monkeypatch, "admin@test.local")
        admin = crud.get_user_by_email(db_session, "admin@test.local")
        admin.role = "ADMIN"
        db_session.commit()

        resp = client.post(
            f"/api/admin/demo-users/{demo_user.id}/reset",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == demo_user.id
        assert data["email"] == demo_user.email
        assert "cleared" in data
        assert data["cleared"].get("agents") == 1

    def test_reset_returns_404_for_missing_user(self, monkeypatch, client: TestClient, db_session):
        """Endpoint should return 404 for non-existent user."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)
        monkeypatch.setenv("ADMIN_EMAILS", "admin@test.local")

        # Login as super admin
        token = _google_login(client, monkeypatch, "admin@test.local")
        admin = crud.get_user_by_email(db_session, "admin@test.local")
        admin.role = "ADMIN"
        db_session.commit()

        resp = client.post(
            "/api/admin/demo-users/99999/reset",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 404
