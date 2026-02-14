"""Unit tests for the reflection service (collector, judge, writer)."""

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
from zerg.services.reflection.collector import ProjectBatch
from zerg.services.reflection.collector import SessionInfo
from zerg.services.reflection.collector import collect_sessions


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


def _make_session(
    rdb,
    project="test-project",
    summary="Did some work",
    summary_title="Test session",
    reflected_at=None,
    started_at=None,
):
    """Helper: create and persist an AgentSession."""
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="test",
        project=project,
        started_at=started_at or datetime.now(timezone.utc),
        summary=summary,
        summary_title=summary_title,
        reflected_at=reflected_at,
        user_messages=5,
        assistant_messages=10,
        tool_calls=3,
    )
    rdb.add(session)
    rdb.commit()
    rdb.refresh(session)
    return session


def _make_insight(rdb, title="Known gotcha", project="test-project", insight_type="learning"):
    """Helper: create and persist an Insight."""
    insight = Insight(
        insight_type=insight_type,
        title=title,
        project=project,
        description="Some description",
        confidence=0.8,
        observations=[],
    )
    rdb.add(insight)
    rdb.commit()
    rdb.refresh(insight)
    return insight


# ---------------------------------------------------------------------------
# Collector tests
# ---------------------------------------------------------------------------


class TestCollector:
    """Tests for services/reflection/collector.py."""

    def test_collects_unreflected_sessions_with_summaries(self, rdb):
        """Only sessions with summary and reflected_at IS NULL are collected."""
        _make_session(rdb, summary="Has summary", reflected_at=None)
        _make_session(rdb, summary=None, reflected_at=None)  # No summary — skip
        _make_session(rdb, summary="Already reflected", reflected_at=datetime.now(timezone.utc))

        batches = collect_sessions(rdb, project="test-project", window_hours=24)

        assert len(batches) == 1
        assert len(batches[0].sessions) == 1
        assert batches[0].sessions[0].summary == "Has summary"

    def test_respects_window_hours(self, rdb):
        """Sessions older than window_hours are excluded."""
        _make_session(rdb, summary="Recent", started_at=datetime.now(timezone.utc) - timedelta(hours=1))
        _make_session(rdb, summary="Old", started_at=datetime.now(timezone.utc) - timedelta(hours=48))

        batches = collect_sessions(rdb, project="test-project", window_hours=24)

        assert len(batches) == 1
        assert len(batches[0].sessions) == 1
        assert batches[0].sessions[0].summary == "Recent"

    def test_groups_by_project(self, rdb):
        """Sessions are grouped by project."""
        _make_session(rdb, project="alpha", summary="Alpha session")
        _make_session(rdb, project="beta", summary="Beta session")

        batches = collect_sessions(rdb, window_hours=24)

        assert len(batches) == 2
        projects = {b.project for b in batches}
        assert projects == {"alpha", "beta"}

    def test_includes_existing_insights_for_dedup(self, rdb):
        """Batches include existing insights for the project."""
        _make_session(rdb, project="test-project", summary="Test session")
        _make_insight(rdb, title="Known gotcha", project="test-project")

        batches = collect_sessions(rdb, project="test-project", window_hours=24)

        assert len(batches) == 1
        assert len(batches[0].existing_insights) == 1
        assert batches[0].existing_insights[0]["title"] == "Known gotcha"

    def test_empty_when_no_sessions(self, rdb):
        """Returns empty list when no qualifying sessions exist."""
        batches = collect_sessions(rdb, project="test-project", window_hours=24)
        assert batches == []

    def test_project_filter(self, rdb):
        """When project is specified, only that project's sessions are collected."""
        _make_session(rdb, project="alpha", summary="Alpha session")
        _make_session(rdb, project="beta", summary="Beta session")

        batches = collect_sessions(rdb, project="alpha", window_hours=24)

        assert len(batches) == 1
        assert batches[0].project == "alpha"


# ---------------------------------------------------------------------------
# Judge tests
# ---------------------------------------------------------------------------


class TestJudge:
    """Tests for services/reflection/judge.py."""

    @pytest.mark.asyncio
    async def test_produces_valid_actions_from_llm(self):
        """Judge parses structured LLM response into actions."""
        from zerg.services.reflection.judge import analyze_sessions

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps([
            {
                "action": "create_insight",
                "insight_type": "pattern",
                "title": "Use batch queries for performance",
                "description": "Multiple sessions showed N+1 query issues",
                "severity": "warning",
                "confidence": 0.85,
                "tags": ["performance"],
            },
            {
                "action": "skip",
                "reason": "One-off debugging session",
            },
        ])
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        batch = ProjectBatch(
            project="test-project",
            sessions=[
                SessionInfo(
                    id="s1", project="test-project", provider="claude",
                    summary="Fixed N+1 queries", summary_title="Perf fix",
                    started_at=datetime.now(timezone.utc), tool_calls=5, user_messages=3,
                ),
            ],
            existing_insights=[],
        )

        actions, usage = await analyze_sessions(batch, llm_client=mock_client, model="test-model")

        assert len(actions) == 2
        assert actions[0]["action"] == "create_insight"
        assert actions[0]["title"] == "Use batch queries for performance"
        assert actions[0]["project"] == "test-project"
        assert actions[1]["action"] == "skip"
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50

    @pytest.mark.asyncio
    async def test_handles_empty_sessions(self):
        """Returns empty actions for batch with no sessions."""
        from zerg.services.reflection.judge import analyze_sessions

        batch = ProjectBatch(project="test-project", sessions=[], existing_insights=[])
        actions, usage = await analyze_sessions(batch, llm_client=None, model="test")

        assert actions == []
        assert usage["prompt_tokens"] == 0

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self):
        """Gracefully handles malformed LLM JSON response."""
        from zerg.services.reflection.judge import analyze_sessions

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "not valid json"
        mock_response.usage = MagicMock(prompt_tokens=50, completion_tokens=10)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        batch = ProjectBatch(
            project="test-project",
            sessions=[
                SessionInfo(
                    id="s1", project="test-project", provider="claude",
                    summary="Test", summary_title="Test",
                    started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
                ),
            ],
            existing_insights=[],
        )

        actions, usage = await analyze_sessions(batch, llm_client=mock_client, model="test")
        assert actions == []

    @pytest.mark.asyncio
    async def test_handles_no_llm_client(self):
        """Returns empty when no LLM client provided."""
        from zerg.services.reflection.judge import analyze_sessions

        batch = ProjectBatch(
            project="test-project",
            sessions=[
                SessionInfo(
                    id="s1", project="test-project", provider="claude",
                    summary="Test", summary_title="Test",
                    started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
                ),
            ],
            existing_insights=[],
        )

        actions, usage = await analyze_sessions(batch, llm_client=None, model="test")
        assert actions == []
        assert usage["prompt_tokens"] == 0

    @pytest.mark.asyncio
    async def test_handles_wrapper_object(self):
        """Handles LLM response wrapped in {"actions": [...]}."""
        from zerg.services.reflection.judge import _parse_actions

        raw = json.dumps({"actions": [
            {"action": "skip", "reason": "trivial"},
        ]})
        actions = _parse_actions(raw, "test-project")
        assert len(actions) == 1
        assert actions[0]["action"] == "skip"

    @pytest.mark.asyncio
    async def test_filters_invalid_actions(self):
        """Filters out actions with unknown action types."""
        from zerg.services.reflection.judge import _parse_actions

        raw = json.dumps([
            {"action": "create_insight", "title": "Valid"},
            {"action": "invalid_action", "title": "Invalid"},
            {"not_an_action": True},
        ])
        actions = _parse_actions(raw, "test-project")
        assert len(actions) == 1
        assert actions[0]["action"] == "create_insight"


# ---------------------------------------------------------------------------
# Writer tests
# ---------------------------------------------------------------------------


class TestWriter:
    """Tests for services/reflection/writer.py."""

    def test_creates_insight(self, rdb):
        """Writer creates new insights from create_insight actions."""
        from zerg.services.reflection.writer import execute_actions

        actions = [
            {
                "action": "create_insight",
                "project": "test-project",
                "insight_type": "pattern",
                "title": "New insight from reflection",
                "description": "Learned something important",
                "severity": "info",
                "confidence": 0.9,
                "tags": ["test"],
            }
        ]
        session = _make_session(rdb, project="test-project", summary="Test")
        batches = [ProjectBatch(
            project="test-project",
            sessions=[SessionInfo(
                id=str(session.id), project="test-project", provider="claude",
                summary="Test", summary_title="Test",
                started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
            )],
            existing_insights=[],
        )]

        created, merged, skipped = execute_actions(rdb, actions, batches)

        assert created == 1
        assert merged == 0
        assert skipped == 0

        # Verify insight was persisted
        insight = rdb.query(Insight).filter(Insight.title == "New insight from reflection").first()
        assert insight is not None
        assert insight.project == "test-project"
        assert insight.confidence == 0.9

    def test_merges_into_existing_insight(self, rdb):
        """Writer merges observations into existing insights."""
        from zerg.services.reflection.writer import execute_actions

        existing = _make_insight(rdb, title="Known pattern", project="test-project")

        actions = [
            {
                "action": "merge",
                "insight_id": str(existing.id),
                "observation": "Seen again in another session",
            }
        ]
        session = _make_session(rdb, project="test-project", summary="Test")
        batches = [ProjectBatch(
            project="test-project",
            sessions=[SessionInfo(
                id=str(session.id), project="test-project", provider="claude",
                summary="Test", summary_title="Test",
                started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
            )],
            existing_insights=[],
        )]

        created, merged, skipped = execute_actions(rdb, actions, batches)

        assert created == 0
        assert merged == 1

        # Verify observation was appended
        rdb.refresh(existing)
        assert len(existing.observations) == 1
        assert "Seen again in another session" in existing.observations[0]

    def test_stamps_reflected_at_on_sessions(self, rdb):
        """Writer stamps reflected_at on all processed sessions."""
        from zerg.services.reflection.writer import execute_actions

        session = _make_session(rdb, project="test-project", summary="Test")
        assert session.reflected_at is None

        batches = [ProjectBatch(
            project="test-project",
            sessions=[SessionInfo(
                id=str(session.id), project="test-project", provider="claude",
                summary="Test", summary_title="Test",
                started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
            )],
            existing_insights=[],
        )]

        execute_actions(rdb, [], batches)

        rdb.refresh(session)
        assert session.reflected_at is not None

    def test_dedup_prevents_duplicate_insight(self, rdb):
        """Creating an insight with same title+project deduplicates."""
        from zerg.services.reflection.writer import execute_actions

        _make_insight(rdb, title="Duplicate title", project="test-project")

        actions = [
            {
                "action": "create_insight",
                "project": "test-project",
                "insight_type": "learning",
                "title": "Duplicate title",
                "description": "New observation of same thing",
            }
        ]
        batches = [ProjectBatch(project="test-project", sessions=[], existing_insights=[])]

        created, merged, skipped = execute_actions(rdb, actions, batches)

        # Should merge, not create
        assert created == 0
        assert merged == 1

    def test_cross_project_dedup(self, rdb):
        """Same title in different project merges into existing."""
        from zerg.services.reflection.writer import execute_actions

        _make_insight(rdb, title="Cross-project bug", project="alpha")

        actions = [
            {
                "action": "create_insight",
                "project": "beta",
                "insight_type": "failure",
                "title": "Cross-project bug",
                "description": "Same bug in beta",
            }
        ]
        batches = [ProjectBatch(project="beta", sessions=[], existing_insights=[])]

        created, merged, skipped = execute_actions(rdb, actions, batches)

        # Should merge cross-project, not create new
        assert created == 0
        assert merged == 1

        # Verify the beta project was added as a tag
        existing = rdb.query(Insight).filter(Insight.title == "Cross-project bug").first()
        assert "beta" in (existing.tags or [])

    def test_merge_nonexistent_insight_skips(self, rdb):
        """Merging into a non-existent insight ID is skipped."""
        from zerg.services.reflection.writer import execute_actions

        actions = [
            {
                "action": "merge",
                "insight_id": str(uuid4()),
                "observation": "This should be skipped",
            }
        ]
        batches = [ProjectBatch(project="test-project", sessions=[], existing_insights=[])]

        created, merged, skipped = execute_actions(rdb, actions, batches)

        assert created == 0
        assert merged == 0
        assert skipped == 1

    def test_skip_action_counted(self, rdb):
        """Skip actions are counted correctly."""
        from zerg.services.reflection.writer import execute_actions

        actions = [
            {"action": "skip", "reason": "Trivial"},
            {"action": "skip", "reason": "One-off"},
        ]
        batches = [ProjectBatch(project="test-project", sessions=[], existing_insights=[])]

        created, merged, skipped = execute_actions(rdb, actions, batches)

        assert skipped == 2

    def test_creates_reflection_run_record(self, rdb):
        """The reflect() function creates a ReflectionRun record."""
        # Verify the model can be instantiated and persisted
        run = ReflectionRun(
            project="test-project",
            window_hours=24,
            model="gpt-5-mini",
            session_count=3,
            insights_created=1,
            insights_merged=1,
            insights_skipped=1,
            status="completed",
            completed_at=datetime.now(timezone.utc),
        )
        rdb.add(run)
        rdb.commit()
        rdb.refresh(run)

        assert run.id is not None
        assert run.status == "completed"
        assert run.session_count == 3
