"""Integration tests for the session summarization + briefing pipeline.

Tests cover:
    1. Full pipeline: seed session → build transcript → summarize → persist → briefing
    2. Briefing with no sessions (empty project)
    3. Briefing with unsummarized sessions (metadata fallback)
    4. Briefing respects limit parameter
    5. Ingest → background summary flow (mocked LLM)
    6. _generate_summary_background idempotency and guards
    7. BriefingResponse model validation
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


# =====================================================================
# Helpers
# =====================================================================


def _setup_db(tmp_path):
    """Set up a fresh SQLite test database with agent tables."""
    db_path = tmp_path / "briefing_e2e.db"
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _ts(hours_ago: float = 0) -> datetime:
    """Return a UTC datetime offset from now by the given hours."""
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def _seed_session(
    db,
    *,
    project: str = "zerg",
    provider: str = "claude",
    hours_ago: float = 1,
    user_messages: int = 5,
    summary: str | None = None,
    summary_title: str | None = None,
    num_events: int = 3,
) -> AgentSession:
    """Create a session with optional summary and events."""
    session = AgentSession(
        provider=provider,
        environment="production",
        project=project,
        started_at=_ts(hours_ago),
        summary=summary,
        summary_title=summary_title,
        user_messages=user_messages,
        assistant_messages=user_messages + 2,
        tool_calls=user_messages,
    )
    db.add(session)
    db.flush()

    # Add events if requested
    base_ts = _ts(hours_ago)
    for i in range(num_events):
        role = ["user", "assistant", "tool"][i % 3]
        event = AgentEvent(
            session_id=session.id,
            role=role,
            content_text=f"Event {i} content for session" if role != "tool" else None,
            tool_name="Bash" if role == "tool" else None,
            tool_output_text="command output" if role == "tool" else None,
            timestamp=base_ts + timedelta(minutes=i),
        )
        db.add(event)

    db.commit()
    return session


def _mock_llm_client(response_content: str) -> AsyncMock:
    """Create a mock AsyncOpenAI client returning the given JSON content."""
    client = AsyncMock()
    mock_choice = MagicMock()
    mock_choice.message.content = response_content
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    client.chat.completions.create = AsyncMock(return_value=mock_response)
    return client


# =====================================================================
# Test 1: Full pipeline — seed → transcript → summarize → briefing
# =====================================================================


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_seed_summarize_and_briefing(self, tmp_path):
        """Full pipeline: seed session with events, summarize, verify in DB, format briefing."""
        from zerg.routers.agents import _format_age
        from zerg.services.session_processing import build_transcript
        from zerg.services.session_processing import quick_summary

        db = _setup_db(tmp_path)

        # Step 1: Seed a session with events
        session = _seed_session(
            db,
            project="zerg",
            hours_ago=2,
            user_messages=5,
            num_events=6,
        )
        session_id = session.id

        # Step 2: Build transcript from events
        events = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .order_by(AgentEvent.timestamp)
            .all()
        )
        event_dicts = [
            {
                "role": e.role,
                "content_text": e.content_text,
                "tool_name": e.tool_name,
                "tool_output_text": e.tool_output_text,
                "timestamp": e.timestamp,
                "session_id": str(e.session_id),
            }
            for e in events
        ]

        transcript = build_transcript(event_dicts, include_tool_calls=False, token_budget=8000)
        transcript.metadata = {"project": "zerg", "provider": "claude"}

        # Step 3: Call quick_summary with mocked LLM
        mock_client = _mock_llm_client(
            '{"title": "Fix Auth Bug In Login", "summary": "Fixed the authentication bug by adding password validation to login.py."}'
        )
        result = await quick_summary(transcript, mock_client, model="test-model")

        assert result.title == "Fix Auth Bug In Login"
        assert "password validation" in result.summary

        # Step 4: Persist summary to session
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        session.summary = result.summary
        session.summary_title = result.title
        db.commit()

        # Step 5: Query like briefing endpoint does
        sessions = (
            db.query(AgentSession)
            .filter(
                AgentSession.project == "zerg",
                AgentSession.summary.isnot(None),
            )
            .order_by(AgentSession.started_at.desc())
            .limit(5)
            .all()
        )

        assert len(sessions) == 1
        s = sessions[0]

        # Step 6: Format briefing output
        age = _format_age(s.started_at)
        title = s.summary_title or "Untitled"
        line = f"- {age}: {title} -- {s.summary}"

        assert "Fix Auth Bug In Login" in line
        assert "password validation" in line
        assert "2h ago" in age

        db.close()


# =====================================================================
# Test 2: Briefing with no sessions
# =====================================================================


class TestBriefingEmpty:
    def test_empty_project_returns_no_sessions(self, tmp_path):
        """Querying briefing for a non-existent project returns empty."""
        db = _setup_db(tmp_path)

        sessions = (
            db.query(AgentSession)
            .filter(
                AgentSession.project == "nonexistent",
                AgentSession.summary.isnot(None),
            )
            .order_by(AgentSession.started_at.desc())
            .limit(5)
            .all()
        )

        assert len(sessions) == 0
        db.close()


# =====================================================================
# Test 3: Briefing with unsummarized sessions (metadata fallback)
# =====================================================================


class TestBriefingUnsummarized:
    def test_unsummarized_sessions_excluded_from_briefing(self, tmp_path):
        """Sessions without summaries should be excluded from the briefing query."""
        db = _setup_db(tmp_path)

        # Seed sessions: 2 with summary, 3 without
        _seed_session(db, summary="Did auth work.", summary_title="Auth Work", hours_ago=1)
        _seed_session(db, summary="Fixed bug.", summary_title="Bug Fix", hours_ago=2)
        _seed_session(db, hours_ago=3)  # no summary
        _seed_session(db, hours_ago=4)  # no summary
        _seed_session(db, hours_ago=5)  # no summary

        sessions = (
            db.query(AgentSession)
            .filter(
                AgentSession.project == "zerg",
                AgentSession.summary.isnot(None),
            )
            .order_by(AgentSession.started_at.desc())
            .limit(10)
            .all()
        )

        assert len(sessions) == 2
        titles = {s.summary_title for s in sessions}
        assert titles == {"Auth Work", "Bug Fix"}

        db.close()


# =====================================================================
# Test 4: Briefing respects limit
# =====================================================================


class TestBriefingLimit:
    def test_limit_parameter(self, tmp_path):
        """Briefing should respect the limit parameter."""
        db = _setup_db(tmp_path)

        # Seed 10 sessions with summaries
        for i in range(10):
            _seed_session(
                db,
                summary=f"Session {i} summary.",
                summary_title=f"Session {i}",
                hours_ago=float(i),
            )

        # Query with limit=3
        sessions = (
            db.query(AgentSession)
            .filter(
                AgentSession.project == "zerg",
                AgentSession.summary.isnot(None),
            )
            .order_by(AgentSession.started_at.desc())
            .limit(3)
            .all()
        )

        assert len(sessions) == 3
        # Most recent first
        assert sessions[0].summary_title == "Session 0"
        assert sessions[1].summary_title == "Session 1"
        assert sessions[2].summary_title == "Session 2"

        db.close()


# =====================================================================
# Test 5: Ingest → background summary flow (mocked LLM)
# =====================================================================


class TestIngestSummaryFlow:
    def test_ingest_creates_session(self, tmp_path):
        """Ingest should create a session that can later be summarized."""
        db = _setup_db(tmp_path)

        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="production",
                project="zerg",
                started_at=_ts(1),
                events=[
                    EventIngest(
                        role="user",
                        content_text="Fix the login bug",
                        timestamp=_ts(1),
                        source_path="/tmp/test.jsonl",
                        source_offset=0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="I'll fix the login bug now.",
                        timestamp=_ts(1) + timedelta(seconds=30),
                        source_path="/tmp/test.jsonl",
                        source_offset=1,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="Done. Fixed the password validation.",
                        timestamp=_ts(1) + timedelta(minutes=5),
                        source_path="/tmp/test.jsonl",
                        source_offset=2,
                    ),
                ],
            )
        )

        assert result.session_created is True
        assert result.events_inserted == 3

        # Session exists but has no summary yet
        session = db.query(AgentSession).filter(AgentSession.id == result.session_id).first()
        assert session is not None
        assert session.summary is None
        assert session.summary_title is None

        db.close()


# =====================================================================
# Test 6: _generate_summary_background guards and idempotency
# =====================================================================


class TestGenerateSummaryBackground:
    """Tests for _generate_summary_background.

    Patching strategy:
    - ``get_settings`` is imported at module level in ``agents.py`` →
      patch ``zerg.routers.agents.get_settings``
    - ``get_session_factory`` is imported inside the function →
      patch ``zerg.database.get_session_factory``
    - ``AsyncOpenAI`` is imported inside the function →
      patch ``openai.AsyncOpenAI``
    """

    @pytest.mark.asyncio
    async def test_skips_when_summary_already_exists(self, tmp_path):
        """Background task should skip sessions that already have a summary."""
        db = _setup_db(tmp_path)
        session = _seed_session(
            db,
            summary="Already summarized.",
            summary_title="Done",
            num_events=5,
        )

        from zerg.routers.agents import _generate_summary_background

        factory = sessionmaker(bind=db.get_bind())

        mock_settings = MagicMock()
        mock_settings.testing = False
        mock_settings.llm_disabled = False
        mock_settings.openai_api_key = None

        with (
            patch("zerg.database.get_session_factory", return_value=factory),
            patch("zerg.routers.agents.get_settings", return_value=mock_settings),
            patch.dict("os.environ", {"ZAI_API_KEY": "test-key"}),
        ):
            await _generate_summary_background(str(session.id))

        # Summary should NOT have changed
        db.refresh(session)
        assert session.summary == "Already summarized."
        assert session.summary_title == "Done"

        db.close()

    @pytest.mark.asyncio
    async def test_skips_when_no_api_key(self, tmp_path):
        """Background task should skip when no LLM API key is configured."""
        db = _setup_db(tmp_path)
        session = _seed_session(db, num_events=5)

        from zerg.routers.agents import _generate_summary_background

        factory = sessionmaker(bind=db.get_bind())

        mock_settings = MagicMock()
        mock_settings.testing = False
        mock_settings.llm_disabled = False
        mock_settings.openai_api_key = None

        import os

        old_key = os.environ.pop("ZAI_API_KEY", None)
        try:
            with (
                patch("zerg.database.get_session_factory", return_value=factory),
                patch("zerg.routers.agents.get_settings", return_value=mock_settings),
            ):
                await _generate_summary_background(str(session.id))
        finally:
            if old_key:
                os.environ["ZAI_API_KEY"] = old_key

        # Session should still have no summary
        db.refresh(session)
        assert session.summary is None

        db.close()

    @pytest.mark.asyncio
    async def test_skips_when_no_events(self, tmp_path):
        """Background task should skip when session has no events."""
        db = _setup_db(tmp_path)

        # Create session with zero events
        session = AgentSession(
            provider="claude",
            environment="production",
            project="zerg",
            started_at=_ts(1),
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
        )
        db.add(session)
        db.commit()

        from zerg.routers.agents import _generate_summary_background

        factory = sessionmaker(bind=db.get_bind())

        mock_settings = MagicMock()
        mock_settings.testing = False
        mock_settings.llm_disabled = False
        mock_settings.openai_api_key = None

        with (
            patch("zerg.database.get_session_factory", return_value=factory),
            patch("zerg.routers.agents.get_settings", return_value=mock_settings),
            patch.dict("os.environ", {"ZAI_API_KEY": "test-key"}),
        ):
            await _generate_summary_background(str(session.id))

        db.refresh(session)
        assert session.summary is None

        db.close()

    @pytest.mark.asyncio
    async def test_generates_summary_with_mocked_llm(self, tmp_path):
        """Background task should generate and persist summary with mocked LLM."""
        db = _setup_db(tmp_path)

        # Seed session with enough events for a transcript
        session = _seed_session(db, num_events=6)
        assert session.summary is None

        from zerg.routers.agents import _generate_summary_background

        factory = sessionmaker(bind=db.get_bind())

        mock_client = _mock_llm_client(
            '{"title": "Fix Login Bug", "summary": "Fixed the login validation issue in auth.py."}'
        )

        mock_settings = MagicMock()
        mock_settings.testing = False
        mock_settings.llm_disabled = False
        mock_settings.openai_api_key = None

        with (
            patch("zerg.database.get_session_factory", return_value=factory),
            patch("zerg.routers.agents.get_settings", return_value=mock_settings),
            patch("openai.AsyncOpenAI", return_value=mock_client),
            patch.dict("os.environ", {"ZAI_API_KEY": "test-key"}),
        ):
            await _generate_summary_background(str(session.id))

        db.refresh(session)
        assert session.summary is not None
        assert "login validation" in session.summary
        assert session.summary_title == "Fix Login Bug"

        db.close()

    @pytest.mark.asyncio
    async def test_handles_llm_error_gracefully(self, tmp_path):
        """Background task should catch LLM errors and not crash."""
        db = _setup_db(tmp_path)
        session = _seed_session(db, num_events=6)

        from zerg.routers.agents import _generate_summary_background

        factory = sessionmaker(bind=db.get_bind())

        # Mock client that raises an exception
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("LLM API down"))

        mock_settings = MagicMock()
        mock_settings.testing = False
        mock_settings.llm_disabled = False
        mock_settings.openai_api_key = None

        with (
            patch("zerg.database.get_session_factory", return_value=factory),
            patch("zerg.routers.agents.get_settings", return_value=mock_settings),
            patch("openai.AsyncOpenAI", return_value=mock_client),
            patch.dict("os.environ", {"ZAI_API_KEY": "test-key"}),
        ):
            # Should not raise
            await _generate_summary_background(str(session.id))

        # Session should still have no summary (error was caught)
        db.refresh(session)
        assert session.summary is None

        db.close()

    @pytest.mark.asyncio
    async def test_skips_nonexistent_session(self, tmp_path):
        """Background task should handle missing session gracefully."""
        from zerg.routers.agents import _generate_summary_background

        db = _setup_db(tmp_path)
        factory = sessionmaker(bind=db.get_bind())

        mock_settings = MagicMock()
        mock_settings.testing = False
        mock_settings.llm_disabled = False
        mock_settings.openai_api_key = None

        with (
            patch("zerg.database.get_session_factory", return_value=factory),
            patch("zerg.routers.agents.get_settings", return_value=mock_settings),
            patch.dict("os.environ", {"ZAI_API_KEY": "test-key"}),
        ):
            # Should not raise for non-existent UUID
            await _generate_summary_background(str(uuid4()))

        db.close()


# =====================================================================
# Test 7: BriefingResponse model
# =====================================================================


class TestBriefingResponseModel:
    def test_briefing_response_with_data(self):
        """BriefingResponse serializes correctly with summary data."""
        from zerg.routers.agents import BriefingResponse

        resp = BriefingResponse(
            project="zerg",
            session_count=3,
            briefing="- 2h ago: Fix Auth -- Fixed auth bug.\n- 5h ago: Add Search -- Added search.",
        )

        data = resp.model_dump()
        assert data["project"] == "zerg"
        assert data["session_count"] == 3
        assert "Fix Auth" in data["briefing"]

    def test_briefing_response_empty(self):
        """BriefingResponse serializes correctly with no briefing."""
        from zerg.routers.agents import BriefingResponse

        resp = BriefingResponse(
            project="empty-project",
            session_count=0,
            briefing=None,
        )

        data = resp.model_dump()
        assert data["project"] == "empty-project"
        assert data["session_count"] == 0
        assert data["briefing"] is None


# =====================================================================
# Test 8: _format_age edge cases
# =====================================================================


class TestFormatAgeEdgeCases:
    def test_future_timestamp(self):
        """_format_age handles future timestamps gracefully."""
        from zerg.routers.agents import _format_age

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert _format_age(future) == "just now"

    def test_exact_boundary_hours(self):
        """_format_age at exact hour boundary."""
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(hours=23, minutes=59)
        result = _format_age(t)
        assert result == "23h ago"

    def test_exact_day_boundary(self):
        """_format_age at exact day boundary."""
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(days=1)
        result = _format_age(t)
        assert result == "yesterday"


# =====================================================================
# Test 9: Multiple projects in briefing (isolation)
# =====================================================================


class TestProjectIsolation:
    def test_briefing_isolates_projects(self, tmp_path):
        """Briefing for one project doesn't include sessions from another."""
        db = _setup_db(tmp_path)

        _seed_session(db, project="zerg", summary="Zerg work.", summary_title="Zerg Session", hours_ago=1)
        _seed_session(db, project="hdr", summary="HDR work.", summary_title="HDR Session", hours_ago=2)
        _seed_session(db, project="life-hub", summary="Life work.", summary_title="Life Session", hours_ago=3)

        zerg_sessions = (
            db.query(AgentSession)
            .filter(AgentSession.project == "zerg", AgentSession.summary.isnot(None))
            .all()
        )
        hdr_sessions = (
            db.query(AgentSession)
            .filter(AgentSession.project == "hdr", AgentSession.summary.isnot(None))
            .all()
        )

        assert len(zerg_sessions) == 1
        assert zerg_sessions[0].summary_title == "Zerg Session"
        assert len(hdr_sessions) == 1
        assert hdr_sessions[0].summary_title == "HDR Session"

        db.close()


# =====================================================================
# Test 10: Briefing ordering (most recent first)
# =====================================================================


class TestBriefingOrdering:
    def test_most_recent_first(self, tmp_path):
        """Briefing sessions should be ordered most recent first."""
        db = _setup_db(tmp_path)

        _seed_session(db, summary="Old work.", summary_title="Old", hours_ago=10)
        _seed_session(db, summary="Recent work.", summary_title="Recent", hours_ago=1)
        _seed_session(db, summary="Middle work.", summary_title="Middle", hours_ago=5)

        sessions = (
            db.query(AgentSession)
            .filter(AgentSession.project == "zerg", AgentSession.summary.isnot(None))
            .order_by(AgentSession.started_at.desc())
            .limit(10)
            .all()
        )

        titles = [s.summary_title for s in sessions]
        assert titles == ["Recent", "Middle", "Old"]

        db.close()
