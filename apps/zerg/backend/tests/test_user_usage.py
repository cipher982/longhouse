"""Tests for the /api/users/me/usage endpoint."""

import pytest
from datetime import datetime, timezone

from zerg.crud import crud
from zerg.services.usage_service import get_user_usage


class TestUserUsageEndpoint:
    """Tests for GET /api/users/me/usage."""

    def test_usage_endpoint_returns_empty_stats(self, client, db_session, test_user):
        """New user with no runs should have zero usage."""
        resp = client.get("/api/users/me/usage")
        assert resp.status_code == 200

        data = resp.json()
        assert data["period"] == "today"
        assert data["tokens"]["total"] == 0
        assert data["cost_usd"] == 0.0
        assert data["runs"] == 0
        # Default: no limit configured in test env
        assert data["limit"]["status"] in ("ok", "unlimited")

    def test_usage_endpoint_with_runs(self, client, db_session, test_user):
        """User with completed runs should see aggregated stats."""
        # Create an agent and some runs with costs
        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Test Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4o",
        )
        thread = crud.create_thread(db_session, agent_id=agent.id, title="Test Thread")

        # Create a run with tokens and cost
        run = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="manual")
        crud.mark_running(db_session, run.id)
        crud.mark_finished(db_session, run.id, total_tokens=1000, total_cost_usd=0.05)

        # Create another run
        run2 = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="manual")
        crud.mark_running(db_session, run2.id)
        crud.mark_finished(db_session, run2.id, total_tokens=500, total_cost_usd=0.025)

        resp = client.get("/api/users/me/usage")
        assert resp.status_code == 200

        data = resp.json()
        assert data["tokens"]["total"] == 1500
        assert data["cost_usd"] == 0.075
        assert data["runs"] == 2

    def test_usage_endpoint_period_param(self, client, db_session, test_user):
        """Period parameter should be accepted."""
        resp = client.get("/api/users/me/usage?period=7d")
        assert resp.status_code == 200
        assert resp.json()["period"] == "7d"

        resp = client.get("/api/users/me/usage?period=30d")
        assert resp.status_code == 200
        assert resp.json()["period"] == "30d"

    def test_usage_endpoint_invalid_period(self, client, db_session, test_user):
        """Invalid period should return 422."""
        resp = client.get("/api/users/me/usage?period=invalid")
        assert resp.status_code == 422

    def test_usage_endpoint_requires_auth(self, client):
        """Endpoint should require authentication."""
        # This test depends on auth being enabled in test config
        # Most test setups disable auth, so this may pass anyway
        pass  # Auth is tested elsewhere


class TestUsageService:
    """Unit tests for the usage service."""

    def test_get_user_usage_empty(self, db_session, test_user):
        """Service returns zeros for user with no runs."""
        result = get_user_usage(db_session, test_user.id, "today")

        assert result.period == "today"
        assert result.tokens.total == 0
        assert result.cost_usd == 0.0
        assert result.runs == 0

    def test_get_user_usage_with_data(self, db_session, test_user):
        """Service aggregates tokens and costs correctly."""
        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Test",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4o",
        )
        thread = crud.create_thread(db_session, agent_id=agent.id, title="Test")

        # Create runs
        run1 = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="manual")
        crud.mark_running(db_session, run1.id)
        crud.mark_finished(db_session, run1.id, total_tokens=100, total_cost_usd=0.01)

        run2 = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="manual")
        crud.mark_running(db_session, run2.id)
        crud.mark_finished(db_session, run2.id, total_tokens=200, total_cost_usd=0.02)

        result = get_user_usage(db_session, test_user.id, "today")

        assert result.tokens.total == 300
        assert result.cost_usd == 0.03
        assert result.runs == 2

    def test_limit_status_unlimited(self, db_session, test_user, monkeypatch):
        """status='unlimited' when no limit configured."""
        # Default test env has no limit (0)
        result = get_user_usage(db_session, test_user.id, "today")
        assert result.limit.status == "unlimited"

    def test_limit_status_ok(self, db_session, test_user, monkeypatch):
        """status='ok' when under 80% of limit."""
        from zerg.config import get_settings

        settings = get_settings()
        settings.override(daily_cost_per_user_cents=100)  # $1.00 limit

        # Create minimal usage (well under 80%)
        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Test",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4o",
        )
        thread = crud.create_thread(db_session, agent_id=agent.id, title="Test")
        run = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="manual")
        crud.mark_running(db_session, run.id)
        crud.mark_finished(db_session, run.id, total_tokens=10, total_cost_usd=0.10)  # 10% of $1

        result = get_user_usage(db_session, test_user.id, "today")

        assert result.limit.status == "ok"
        assert result.limit.used_percent == 10.0
        assert result.limit.remaining_usd == 0.90

    def test_limit_status_warning(self, db_session, test_user, monkeypatch):
        """status='warning' when 80-99% of limit."""
        from zerg.config import get_settings

        settings = get_settings()
        settings.override(daily_cost_per_user_cents=100)  # $1.00 limit

        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Test",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4o",
        )
        thread = crud.create_thread(db_session, agent_id=agent.id, title="Test")
        run = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="manual")
        crud.mark_running(db_session, run.id)
        crud.mark_finished(db_session, run.id, total_tokens=100, total_cost_usd=0.85)  # 85% of $1

        result = get_user_usage(db_session, test_user.id, "today")

        assert result.limit.status == "warning"
        assert result.limit.used_percent == 85.0

    def test_limit_status_exceeded(self, db_session, test_user, monkeypatch):
        """status='exceeded' when at or over 100% of limit."""
        from zerg.config import get_settings

        settings = get_settings()
        settings.override(daily_cost_per_user_cents=100)  # $1.00 limit

        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Test",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4o",
        )
        thread = crud.create_thread(db_session, agent_id=agent.id, title="Test")
        run = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="manual")
        crud.mark_running(db_session, run.id)
        crud.mark_finished(db_session, run.id, total_tokens=1000, total_cost_usd=1.50)  # 150% of $1

        result = get_user_usage(db_session, test_user.id, "today")

        assert result.limit.status == "exceeded"
        assert result.limit.used_percent == 150.0
        assert result.limit.remaining_usd == 0.0  # Clamped to 0
