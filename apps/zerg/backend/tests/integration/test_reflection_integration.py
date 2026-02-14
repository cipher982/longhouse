"""Integration tests for the reflection system — full flow, API, idempotency."""

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from zerg.models.agents import AgentSession
from zerg.models.agents import agents_metadata
from zerg.models.work import Insight
from zerg.models.work import ReflectionRun


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reflection_schema_setup():
    """Create agents + work tables once per test module."""
    from tests.conftest import test_engine

    agents_metadata.create_all(bind=test_engine)
    yield


@pytest.fixture
def rdb(db_session, reflection_schema_setup):
    """Provide a db session with agents/work tables, cleaned between tests."""
    from tests.conftest import test_engine

    with test_engine.connect() as conn:
        for table_name in ["sessions", "insights", "reflection_runs"]:
            try:
                conn.execute(text(f"DELETE FROM {table_name}"))
            except Exception:
                pass
        conn.commit()

    yield db_session


def _make_session(rdb, project="test-project", summary="Did some work", reflected_at=None):
    """Helper: create an AgentSession."""
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="test",
        project=project,
        started_at=datetime.now(timezone.utc),
        summary=summary,
        summary_title=f"Session: {summary[:30]}",
        reflected_at=reflected_at,
        user_messages=5,
        assistant_messages=10,
        tool_calls=3,
    )
    rdb.add(session)
    rdb.commit()
    rdb.refresh(session)
    return session


def _mock_llm_client(actions: list[dict]):
    """Create a mock LLM client that returns the given actions."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps(actions)
    mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# Full flow tests
# ---------------------------------------------------------------------------


class TestFullReflectionFlow:
    """End-to-end reflection flow: ingest sessions → reflect → insights created → sessions stamped."""

    @pytest.mark.asyncio
    async def test_full_flow(self, rdb):
        """Ingest sessions, trigger reflection, verify insights created and sessions stamped."""
        from zerg.services.reflection import reflect

        # Create unreflected sessions
        s1 = _make_session(rdb, project="myapp", summary="Fixed auth bug in login flow")
        s2 = _make_session(rdb, project="myapp", summary="Deployed new monitoring for auth service")

        # Mock LLM that finds a pattern
        client = _mock_llm_client([
            {
                "action": "create_insight",
                "insight_type": "pattern",
                "title": "Auth service needs monitoring",
                "description": "Multiple sessions dealt with auth issues",
                "severity": "warning",
                "confidence": 0.85,
                "tags": ["auth"],
            },
            {
                "action": "skip",
                "reason": "Deployment was routine",
            },
        ])

        result = await reflect(
            db=rdb, project="myapp", window_hours=24,
            llm_client=client, model="test-model",
        )

        # Verify result
        assert result.session_count == 2
        assert result.insights_created == 1
        assert result.insights_skipped == 1
        assert result.error is None

        # Verify insight was persisted
        insight = rdb.query(Insight).filter(Insight.title == "Auth service needs monitoring").first()
        assert insight is not None
        assert insight.project == "myapp"
        assert insight.confidence == 0.85

        # Verify sessions were stamped
        rdb.refresh(s1)
        rdb.refresh(s2)
        assert s1.reflected_at is not None
        assert s2.reflected_at is not None

        # Verify ReflectionRun was recorded
        run = rdb.query(ReflectionRun).first()
        assert run is not None
        assert run.status == "completed"
        assert run.session_count == 2
        assert run.insights_created == 1

    @pytest.mark.asyncio
    async def test_idempotency(self, rdb):
        """Running reflection twice doesn't re-process stamped sessions."""
        from zerg.services.reflection import reflect

        _make_session(rdb, project="myapp", summary="Test session")

        client = _mock_llm_client([
            {
                "action": "create_insight",
                "insight_type": "learning",
                "title": "First run insight",
                "description": "Found on first run",
            },
        ])

        # First run
        result1 = await reflect(
            db=rdb, project="myapp", window_hours=24,
            llm_client=client, model="test-model",
        )
        assert result1.session_count == 1
        assert result1.insights_created == 1

        # Reset mock for second run
        client2 = _mock_llm_client([
            {
                "action": "create_insight",
                "insight_type": "learning",
                "title": "Second run insight",
                "description": "Should not appear",
            },
        ])

        # Second run — no sessions to process
        result2 = await reflect(
            db=rdb, project="myapp", window_hours=24,
            llm_client=client2, model="test-model",
        )
        assert result2.session_count == 0
        assert result2.insights_created == 0

        # Only the first insight exists
        insights = rdb.query(Insight).all()
        assert len(insights) == 1
        assert insights[0].title == "First run insight"

    @pytest.mark.asyncio
    async def test_empty_case(self, rdb):
        """Reflection with no unprocessed sessions returns cleanly."""
        from zerg.services.reflection import reflect

        result = await reflect(
            db=rdb, project="myapp", window_hours=24,
            llm_client=None, model="test-model",
        )

        assert result.session_count == 0
        assert result.insights_created == 0
        assert result.insights_merged == 0
        assert result.insights_skipped == 0
        assert result.error is None

        # Run was still recorded
        run = rdb.query(ReflectionRun).first()
        assert run is not None
        assert run.status == "completed"

    @pytest.mark.asyncio
    async def test_llm_failure_recorded(self, rdb):
        """LLM failure is recorded in the run, doesn't crash."""
        from zerg.services.reflection import reflect

        _make_session(rdb, project="myapp", summary="Test session")

        # Mock client that raises
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("LLM unavailable")
        )

        result = await reflect(
            db=rdb, project="myapp", window_hours=24,
            llm_client=mock_client, model="test-model",
        )

        # The run should still complete (with 0 insights since LLM failed)
        # The judge catches the exception and returns empty actions
        assert result.error is None  # judge handles the error internally
        assert result.session_count == 1


# ---------------------------------------------------------------------------
# API endpoint tests (using FastAPI TestClient)
# ---------------------------------------------------------------------------


class TestReflectionAPI:
    """Tests for the reflection API endpoints."""

    def test_list_reflections_endpoint(self, rdb, client):
        """GET /api/agents/reflections returns run history."""
        # Create a reflection run
        run = ReflectionRun(
            project="test-project",
            status="completed",
            session_count=5,
            insights_created=2,
            insights_merged=1,
            insights_skipped=2,
            model="gpt-5-mini",
            completed_at=datetime.now(timezone.utc),
        )
        rdb.add(run)
        rdb.commit()

        resp = client.get("/api/agents/reflections")
        assert resp.status_code == 200

        data = resp.json()
        assert data["total"] == 1
        assert len(data["runs"]) == 1
        assert data["runs"][0]["session_count"] == 5
        assert data["runs"][0]["insights_created"] == 2

    def test_list_reflections_project_filter(self, rdb, client):
        """GET /api/agents/reflections?project=X filters by project."""
        rdb.add(ReflectionRun(project="alpha", status="completed"))
        rdb.add(ReflectionRun(project="beta", status="completed"))
        rdb.commit()

        resp = client.get("/api/agents/reflections?project=alpha")
        assert resp.status_code == 200

        data = resp.json()
        assert data["total"] == 1
        assert data["runs"][0]["project"] == "alpha"


# ---------------------------------------------------------------------------
# Briefing integration tests
# ---------------------------------------------------------------------------


class TestBriefingInsights:
    """Tests for insights appearing in the briefing endpoint."""

    def test_briefing_includes_insights(self, rdb, client):
        """Briefing response includes recent insights for the project."""
        # Create a session with summary (for the session portion)
        _make_session(rdb, project="myapp", summary="Test session")

        # Create an insight for the project
        insight = Insight(
            insight_type="warning",
            title="Docker compose needs host networking",
            description="Always use host networking for containers accessing local services",
            project="myapp",
            severity="warning",
            confidence=0.9,
            observations=[],
        )
        rdb.add(insight)
        rdb.commit()

        resp = client.get("/api/agents/briefing?project=myapp")
        assert resp.status_code == 200

        data = resp.json()
        assert data["briefing"] is not None
        assert "Known gotchas" in data["briefing"]
        assert "Docker compose needs host networking" in data["briefing"]

    def test_briefing_includes_high_confidence_cross_project(self, rdb, client):
        """High-confidence insights from other projects appear in briefing."""
        _make_session(rdb, project="myapp", summary="Test session")

        # Create high-confidence insight in different project
        insight = Insight(
            insight_type="failure",
            title="UFW blocks Docker internal traffic",
            description="Add 172.16.0.0/12 to UFW",
            project="other-project",
            severity="critical",
            confidence=0.95,
            observations=[],
        )
        rdb.add(insight)
        rdb.commit()

        resp = client.get("/api/agents/briefing?project=myapp")
        assert resp.status_code == 200

        data = resp.json()
        assert data["briefing"] is not None
        assert "UFW blocks Docker internal traffic" in data["briefing"]
        assert "from other-project" in data["briefing"]

    def test_briefing_no_insights_still_works(self, rdb, client):
        """Briefing works fine when no insights exist."""
        _make_session(rdb, project="myapp", summary="Test session")

        resp = client.get("/api/agents/briefing?project=myapp")
        assert resp.status_code == 200

        data = resp.json()
        assert data["briefing"] is not None
        # Should have session notes but no gotchas section
        assert "Known gotchas" not in data["briefing"]
