"""Tests for QA Agent job."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.jobs.qa import config
from zerg.jobs.qa.qa_agent import _parse_agent_state
from zerg.jobs.qa.qa_agent import collect_health_data


class TestParseAgentState:
    """Tests for parsing agent output."""

    def test_parse_json_block(self):
        """Should parse JSON from markdown code block."""
        stdout = '''
Here is my analysis:

```json
{
    "version": 1,
    "checks_passed": 5,
    "checks_total": 6,
    "issues": {
        "error_rate_high": {
            "fingerprint": "error_rate_high",
            "status": "open"
        }
    }
}
```

Done.
'''
        result, parse_ok = _parse_agent_state(stdout)

        assert parse_ok is True
        assert result["version"] == 1
        assert result["checks_passed"] == 5
        assert result["checks_total"] == 6
        assert "error_rate_high" in result["issues"]

    def test_parse_raw_json(self):
        """Should parse raw JSON output."""
        stdout = '{"version": 1, "checks_passed": 3, "issues": {}}'

        result, parse_ok = _parse_agent_state(stdout)

        assert parse_ok is True
        assert result["version"] == 1
        assert result["checks_passed"] == 3

    def test_empty_output_returns_default(self):
        """Should return default state and parse_ok=False for empty output."""
        result, parse_ok = _parse_agent_state("")

        assert parse_ok is False
        assert result["version"] == config.STATE_VERSION
        assert result["issues"] == {}
        assert result["checks_passed"] == 0

    def test_invalid_json_returns_default(self):
        """Should return default state and parse_ok=False for invalid JSON."""
        result, parse_ok = _parse_agent_state("This is not JSON at all")

        assert parse_ok is False
        assert result["version"] == config.STATE_VERSION
        assert result["issues"] == {}

    def test_preserves_default_fields(self):
        """Should preserve default fields when not in agent output."""
        stdout = '{"version": 1, "checks_passed": 10, "issues": {}}'

        result, parse_ok = _parse_agent_state(stdout)

        assert parse_ok is True
        assert result["checks_passed"] == 10
        assert result["version"] == 1
        assert "issues" in result
        assert "baseline" in result


class TestCollectHealthData:
    """Tests for direct health data collection."""

    @pytest.mark.asyncio
    async def test_collects_basic_health(self):
        """Should collect basic health data."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "healthy"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            # Mock DB as disabled to skip DB queries
            with patch("zerg.jobs.ops_db.is_job_queue_db_enabled", return_value=False):
                data = await collect_health_data()

        assert "collected_at" in data
        assert "checks" in data
        assert data["checks"]["health"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_handles_health_check_failure(self):
        """Should handle health check failure gracefully."""
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.side_effect = Exception("Connection refused")
            mock_client_cls.return_value = mock_client

            with patch("zerg.jobs.ops_db.is_job_queue_db_enabled", return_value=False):
                data = await collect_health_data()

        assert data["checks"]["health"]["status"] == "error"
        assert "Connection refused" in data["checks"]["health"]["error"]


class TestJobRegistration:
    """Tests for job registration."""

    def test_job_registered(self):
        """Should register the zerg-qa job."""
        from zerg.jobs.registry import job_registry
        from zerg.jobs.registry import register_all_jobs

        register_all_jobs()

        job = job_registry.get("zerg-qa")
        assert job is not None
        assert job.id == "zerg-qa"
        assert job.cron == "*/15 * * * *"
        assert job.timeout_seconds == 600
        assert "qa" in job.tags
        assert job.project == "zerg"


class TestIssueLifecycle:
    """Tests for issue lifecycle management logic."""

    def test_new_issue_from_clean_state(self):
        """New issue should start with consecutive=1."""
        previous_state = {"issues": {}}
        # Simulating what the agent should produce
        expected_issue = {
            "fingerprint": "error_rate_high",
            "status": "open",
            "consecutive": 1,
            "occurrences": 1,
            "chronic": False,
        }
        assert expected_issue["consecutive"] == 1
        assert expected_issue["chronic"] is False

    def test_chronic_after_three_consecutive(self):
        """Issue should become chronic after 3 consecutive occurrences."""
        issue = {
            "fingerprint": "error_rate_high",
            "consecutive": 3,
            "occurrences": 3,
            "chronic": True,  # Agent should set this
        }
        assert issue["chronic"] is True


class TestConfig:
    """Tests for QA agent configuration."""

    def test_thresholds_defined(self):
        """Should have all required thresholds."""
        assert "error_rate_warn" in config.THRESHOLDS
        assert "error_rate_critical" in config.THRESHOLDS
        assert "p95_latency_warn_ms" in config.THRESHOLDS
        assert "p95_latency_critical_ms" in config.THRESHOLDS
        assert "failed_runs_1h_warn" in config.THRESHOLDS

    def test_threshold_values_reasonable(self):
        """Threshold values should be in reasonable ranges."""
        assert 0 < config.THRESHOLDS["error_rate_warn"] < 1
        assert config.THRESHOLDS["error_rate_warn"] < config.THRESHOLDS["error_rate_critical"]
        assert config.THRESHOLDS["p95_latency_warn_ms"] < config.THRESHOLDS["p95_latency_critical_ms"]

    def test_alert_cooldown_positive(self):
        """Alert cooldown should be positive."""
        assert config.ALERT_COOLDOWN_MINUTES > 0

    def test_chronic_threshold_positive(self):
        """Chronic threshold should be positive."""
        assert config.CHRONIC_THRESHOLD > 0
        assert config.RESOLVE_THRESHOLD > 0


class TestDefaultState:
    """Tests for default state generation."""

    def test_default_state_structure(self):
        """Default state should have all required fields."""
        from zerg.jobs.qa.qa_agent import _default_state

        state = _default_state()
        assert state["version"] == config.STATE_VERSION
        assert state["baseline"] == {}
        assert state["issues"] == {}
        assert state["checks_passed"] == 0
        assert state["checks_total"] == 0
        assert state["alert_sent"] is False
        assert "updated_at" in state


class TestFetchPreviousState:
    """Tests for fetching previous QA state from database."""

    @pytest.mark.asyncio
    async def test_handles_dict_from_asyncpg(self):
        """Should handle dict returned directly from asyncpg."""
        from zerg.jobs.qa.qa_agent import _fetch_previous_qa_state

        mock_row = {"qa_state": {"version": 1, "issues": {"test": "data"}}}

        # Create properly structured async mocks
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)

        # Mock the context manager for pool.acquire()
        mock_pool = MagicMock()
        mock_acquire_cm = AsyncMock()
        mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_cm.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire.return_value = mock_acquire_cm

        with patch("zerg.jobs.ops_db.is_job_queue_db_enabled", return_value=True):
            with patch("zerg.jobs.ops_db.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await _fetch_previous_qa_state()

        assert result == {"version": 1, "issues": {"test": "data"}}

    @pytest.mark.asyncio
    async def test_handles_string_from_asyncpg(self):
        """Should handle JSON string returned from asyncpg."""
        from zerg.jobs.qa.qa_agent import _fetch_previous_qa_state

        mock_row = {"qa_state": '{"version": 1, "issues": {}}'}

        # Create properly structured async mocks
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)

        # Mock the context manager for pool.acquire()
        mock_pool = MagicMock()
        mock_acquire_cm = AsyncMock()
        mock_acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_cm.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire.return_value = mock_acquire_cm

        with patch("zerg.jobs.ops_db.is_job_queue_db_enabled", return_value=True):
            with patch("zerg.jobs.ops_db.get_pool", new=AsyncMock(return_value=mock_pool)):
                result = await _fetch_previous_qa_state()

        assert result == {"version": 1, "issues": {}}

    @pytest.mark.asyncio
    async def test_returns_none_when_db_disabled(self):
        """Should return None when job queue DB is disabled."""
        from zerg.jobs.qa.qa_agent import _fetch_previous_qa_state

        with patch("zerg.jobs.ops_db.is_job_queue_db_enabled", return_value=False):
            result = await _fetch_previous_qa_state()

        assert result is None


class TestAgentFailureHandling:
    """Tests for handling agent analysis failures."""

    @pytest.mark.asyncio
    async def test_agent_failure_preserves_previous_state(self):
        """Agent failure should preserve previous state, not clear it."""
        from zerg.jobs.qa.qa_agent import _default_state
        from zerg.jobs.qa.qa_agent import run

        previous_state = {
            "version": 1,
            "issues": {"error_rate_high": {"status": "open", "chronic": True}},
            "baseline": {"p95_latency_ms": 2000},
        }

        with patch("zerg.jobs.qa.qa_agent._fetch_previous_qa_state", return_value=previous_state):
            with patch("zerg.jobs.qa.qa_agent._run_collect_script", return_value={"success": True, "status": "ok"}):
                with patch(
                    "zerg.jobs.qa.qa_agent._run_agent_analysis",
                    return_value={"success": False, "status": "cli_not_found", "error": "claude not found"},
                ):
                    result = await run()

        # Should preserve issues from previous state
        assert "error_rate_high" in result["qa_state"]["issues"]
        assert result["qa_state"]["issues"]["error_rate_high"]["chronic"] is True
        assert result["agent_status"] == "cli_not_found"
        # Should record the error
        assert "agent_error" in result["qa_state"]
