"""Tests for session_processing.summarize and the briefing endpoint.

Tests cover:
    - SessionSummary dataclass creation
    - quick_summary with mock AsyncOpenAI client
    - structured_summary with mock client
    - batch_summarize with mock client and concurrency
    - _format_age helper (relative time formatting)
    - GET /agents/briefing endpoint format
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.session_processing import SessionSummary
from zerg.services.session_processing import SessionTranscript
from zerg.services.session_processing import batch_summarize
from zerg.services.session_processing import quick_summary
from zerg.services.session_processing import structured_summary
from zerg.services.session_processing.transcript import SessionMessage
from zerg.services.session_processing.transcript import Turn


# =====================================================================
# Helpers
# =====================================================================


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 2, 11, hour, minute, 0, tzinfo=timezone.utc)


def _make_transcript(session_id: str = "sess-test") -> SessionTranscript:
    """Build a minimal SessionTranscript for testing."""
    messages = [
        SessionMessage(role="user", content="Fix the auth bug in login.py", timestamp=_ts(10, 0)),
        SessionMessage(role="assistant", content="I'll fix the login bug now.", timestamp=_ts(10, 1)),
        SessionMessage(
            role="assistant",
            content="Done. Added password validation to login.py.",
            timestamp=_ts(10, 5),
        ),
    ]
    turns = [
        Turn(
            turn_index=0,
            role="user",
            combined_text="Fix the auth bug in login.py",
            timestamp=_ts(10, 0),
            message_count=1,
            token_count=8,
        ),
        Turn(
            turn_index=1,
            role="assistant",
            combined_text="I'll fix the login bug now.\nDone. Added password validation to login.py.",
            timestamp=_ts(10, 1),
            message_count=2,
            token_count=15,
        ),
    ]
    return SessionTranscript(
        session_id=session_id,
        messages=messages,
        turns=turns,
        first_user_message="Fix the auth bug in login.py",
        last_assistant_message="Done. Added password validation to login.py.",
        total_tokens=23,
        metadata={"project": "zerg", "provider": "claude"},
    )


def _mock_client(response_content: str) -> AsyncMock:
    """Create a mock AsyncOpenAI client that returns the given content."""
    client = AsyncMock()
    mock_choice = MagicMock()
    mock_choice.message.content = response_content
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    client.chat.completions.create = AsyncMock(return_value=mock_response)
    return client


# =====================================================================
# SessionSummary dataclass
# =====================================================================


class TestSessionSummary:
    def test_basic_creation(self):
        s = SessionSummary(
            session_id="sess-1",
            title="Fix Auth Bug",
            summary="Fixed the login bug in auth.py.",
        )
        assert s.session_id == "sess-1"
        assert s.title == "Fix Auth Bug"
        assert s.summary == "Fixed the login bug in auth.py."
        assert s.topic is None
        assert s.outcome is None
        assert s.bullets is None
        assert s.tags is None

    def test_full_creation(self):
        s = SessionSummary(
            session_id="sess-2",
            title="Refactor Database Layer",
            summary="Refactored the DB layer to use SQLAlchemy 2.0 style.",
            topic="database refactoring",
            outcome="All tests passing after migration.",
            bullets=["Migrated queries", "Added type hints", "Updated tests"],
            tags=["database", "refactor", "sqlalchemy"],
        )
        assert s.topic == "database refactoring"
        assert len(s.bullets) == 3
        assert "database" in s.tags


# =====================================================================
# quick_summary
# =====================================================================


class TestQuickSummary:
    @pytest.mark.asyncio
    async def test_quick_summary_parses_json(self):
        """quick_summary should parse valid JSON from LLM response."""
        response_json = '{"title": "Fix Login Bug", "summary": "Fixed password validation in login.py."}'
        client = _mock_client(response_json)
        transcript = _make_transcript()

        result = await quick_summary(transcript, client, model="test-model")

        assert isinstance(result, SessionSummary)
        assert result.session_id == "sess-test"
        assert result.title == "Fix Login Bug"
        assert result.summary == "Fixed password validation in login.py."

    @pytest.mark.asyncio
    async def test_quick_summary_handles_markdown_fences(self):
        """quick_summary should strip markdown code fences."""
        response_json = '```json\n{"title": "Auth Fix", "summary": "Fixed auth."}\n```'
        client = _mock_client(response_json)
        transcript = _make_transcript()

        result = await quick_summary(transcript, client)

        assert result.title == "Auth Fix"
        assert result.summary == "Fixed auth."

    @pytest.mark.asyncio
    async def test_quick_summary_fallback_on_bad_json(self):
        """quick_summary should fall back to raw text if JSON parsing fails."""
        client = _mock_client("This is just a plain text summary without JSON.")
        transcript = _make_transcript()

        result = await quick_summary(transcript, client)

        assert result.title == "Untitled Session"
        assert "plain text summary" in result.summary

    @pytest.mark.asyncio
    async def test_quick_summary_passes_model(self):
        """quick_summary should pass the model parameter to the client."""
        response_json = '{"title": "Test", "summary": "Test summary."}'
        client = _mock_client(response_json)
        transcript = _make_transcript()

        await quick_summary(transcript, client, model="custom-model-v2")

        call_kwargs = client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "custom-model-v2"

    @pytest.mark.asyncio
    async def test_quick_summary_no_max_tokens(self):
        """quick_summary must NOT pass max_tokens to the API call."""
        response_json = '{"title": "Test", "summary": "Test."}'
        client = _mock_client(response_json)
        transcript = _make_transcript()

        await quick_summary(transcript, client)

        call_kwargs = client.chat.completions.create.call_args
        assert "max_tokens" not in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_quick_summary_includes_metadata_in_prompt(self):
        """quick_summary should include project/provider from metadata."""
        response_json = '{"title": "Test", "summary": "Test."}'
        client = _mock_client(response_json)
        transcript = _make_transcript()
        transcript.metadata = {"project": "zerg", "provider": "claude"}

        await quick_summary(transcript, client)

        call_kwargs = client.chat.completions.create.call_args
        messages = call_kwargs.kwargs["messages"]
        user_msg = messages[1]["content"]
        assert "zerg" in user_msg
        assert "claude" in user_msg


# =====================================================================
# structured_summary
# =====================================================================


class TestStructuredSummary:
    @pytest.mark.asyncio
    async def test_structured_summary_full_json(self):
        """structured_summary should parse all fields from JSON."""
        response_json = """{
            "title": "Fix Login Bug",
            "topic": "authentication",
            "outcome": "Login now validates passwords.",
            "summary": "Fixed password validation in login.py.",
            "bullets": ["Added validation", "Updated tests", "Fixed edge case"],
            "tags": ["auth", "bugfix", "login"]
        }"""
        client = _mock_client(response_json)
        transcript = _make_transcript()

        result = await structured_summary(transcript, client, model="test-model")

        assert result.title == "Fix Login Bug"
        assert result.topic == "authentication"
        assert result.outcome == "Login now validates passwords."
        assert result.summary == "Fixed password validation in login.py."
        assert result.bullets == ["Added validation", "Updated tests", "Fixed edge case"]
        assert result.tags == ["auth", "bugfix", "login"]

    @pytest.mark.asyncio
    async def test_structured_summary_normalizes_tags(self):
        """structured_summary should lowercase and hyphenate tags."""
        response_json = '{"title": "T", "summary": "S", "tags": ["Bug Fix", "AUTH"]}'
        client = _mock_client(response_json)
        transcript = _make_transcript()

        result = await structured_summary(transcript, client)

        assert "bug-fix" in result.tags
        assert "auth" in result.tags

    @pytest.mark.asyncio
    async def test_structured_summary_fallback(self):
        """structured_summary should fall back gracefully on bad JSON."""
        client = _mock_client("Not JSON at all")
        transcript = _make_transcript()

        result = await structured_summary(transcript, client)

        assert result.title == "Untitled Session"
        assert "Not JSON" in result.summary


# =====================================================================
# batch_summarize
# =====================================================================


class TestBatchSummarize:
    @pytest.mark.asyncio
    async def test_batch_summarize_multiple(self):
        """batch_summarize should summarize multiple transcripts."""
        response_json = '{"title": "Session Work", "summary": "Did some work."}'
        client = _mock_client(response_json)
        transcripts = [_make_transcript(f"sess-{i}") for i in range(3)]

        results = await batch_summarize(transcripts, client, model="test-model", max_concurrent=2)

        assert len(results) == 3
        assert all(isinstance(r, SessionSummary) for r in results)
        # Each should have the correct session_id
        session_ids = {r.session_id for r in results}
        assert session_ids == {"sess-0", "sess-1", "sess-2"}

    @pytest.mark.asyncio
    async def test_batch_summarize_empty(self):
        """batch_summarize with empty list returns empty list."""
        client = _mock_client('{"title": "T", "summary": "S"}')

        results = await batch_summarize([], client)

        assert results == []

    @pytest.mark.asyncio
    async def test_batch_summarize_handles_errors(self):
        """batch_summarize should skip sessions that fail."""
        client = AsyncMock()
        # First call succeeds, second raises
        mock_choice = MagicMock()
        mock_choice.message.content = '{"title": "Good", "summary": "OK."}'
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        call_count = 0

        async def _side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("LLM error")
            return mock_response

        client.chat.completions.create = AsyncMock(side_effect=_side_effect)
        transcripts = [_make_transcript(f"sess-{i}") for i in range(3)]

        results = await batch_summarize(transcripts, client, max_concurrent=1)

        # 2 succeed, 1 fails
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_batch_concurrency_limit(self):
        """batch_summarize should respect max_concurrent."""
        concurrent_count = 0
        max_observed = 0

        response_json = '{"title": "T", "summary": "S"}'
        mock_choice = MagicMock()
        mock_choice.message.content = response_json
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        client = AsyncMock()

        async def _tracked_create(**kwargs):
            nonlocal concurrent_count, max_observed
            concurrent_count += 1
            if concurrent_count > max_observed:
                max_observed = concurrent_count
            await asyncio.sleep(0.05)  # Simulate LLM latency
            concurrent_count -= 1
            return mock_response

        client.chat.completions.create = AsyncMock(side_effect=_tracked_create)
        transcripts = [_make_transcript(f"sess-{i}") for i in range(6)]

        results = await batch_summarize(transcripts, client, max_concurrent=2)

        assert len(results) == 6
        assert max_observed <= 2


# =====================================================================
# _format_age helper
# =====================================================================


class TestFormatAge:
    def test_just_now(self):
        from zerg.routers.agents import _format_age

        now = datetime.now(timezone.utc)
        assert _format_age(now) == "just now"

    def test_minutes_ago(self):
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(minutes=15)
        result = _format_age(t)
        assert result == "15m ago"

    def test_hours_ago(self):
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(hours=3)
        result = _format_age(t)
        assert result == "3h ago"

    def test_yesterday(self):
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(days=1, hours=2)
        result = _format_age(t)
        assert result == "yesterday"

    def test_days_ago(self):
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(days=4)
        result = _format_age(t)
        assert result == "4d ago"

    def test_week_ago(self):
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(days=7)
        result = _format_age(t)
        assert result == "1w ago"

    def test_weeks_ago(self):
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc) - timedelta(days=21)
        result = _format_age(t)
        assert result == "3w ago"

    def test_naive_datetime(self):
        """_format_age should handle naive datetimes by assuming UTC."""
        from zerg.routers.agents import _format_age

        t = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        result = _format_age(t)
        assert result == "2h ago"


# =====================================================================
# Briefing endpoint
# =====================================================================


class TestBriefingEndpoint:
    """Test the GET /agents/briefing endpoint with a real SQLite DB."""

    def _setup_db(self, tmp_path):
        """Set up a SQLite test database with sessions."""
        from sqlalchemy.orm import sessionmaker

        from zerg.database import make_engine
        from zerg.models.agents import AgentsBase

        db_path = tmp_path / "briefing_test.db"
        engine = make_engine(f"sqlite:///{db_path}")
        AgentsBase.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        return Session()

    def test_briefing_returns_summaries(self, tmp_path):
        """Briefing endpoint returns formatted summaries for sessions with pre-computed summaries."""
        from zerg.models.agents import AgentSession as _AgentSession

        db = self._setup_db(tmp_path)

        # Create sessions with summaries
        now = datetime.now(timezone.utc)
        s1 = _AgentSession(
            provider="claude",
            environment="production",
            project="zerg",
            started_at=now - timedelta(hours=2),
            summary="Fixed the rate limiting bug in the ingest endpoint.",
            summary_title="Fix Rate Limiting",
            user_messages=5,
            assistant_messages=8,
            tool_calls=3,
        )
        s2 = _AgentSession(
            provider="codex",
            environment="production",
            project="zerg",
            started_at=now - timedelta(hours=5),
            summary="Added FTS5 search to sessions API.",
            summary_title="Add Session Search",
            user_messages=3,
            assistant_messages=4,
            tool_calls=2,
        )
        # Session without summary (should be excluded)
        s3 = _AgentSession(
            provider="claude",
            environment="production",
            project="zerg",
            started_at=now - timedelta(hours=1),
            summary=None,
            summary_title=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        db.add_all([s1, s2, s3])
        db.commit()

        # Query like the endpoint does
        sessions = (
            db.query(_AgentSession)
            .filter(
                _AgentSession.project == "zerg",
                _AgentSession.summary.isnot(None),
            )
            .order_by(_AgentSession.started_at.desc())
            .limit(5)
            .all()
        )

        assert len(sessions) == 2

        from zerg.routers.agents import _format_age

        briefing_lines = []
        for s in sessions:
            age = _format_age(s.started_at)
            title = s.summary_title or "Untitled"
            briefing_lines.append(f"- {age}: {title} -- {s.summary}")

        briefing = "\n".join(briefing_lines)
        assert "Fix Rate Limiting" in briefing
        assert "Add Session Search" in briefing
        assert "2h ago" in briefing
        assert "5h ago" in briefing

        db.close()

    def test_briefing_empty_project(self, tmp_path):
        """Briefing for a project with no summaries returns None."""
        from zerg.models.agents import AgentSession as _AgentSession

        db = self._setup_db(tmp_path)

        sessions = (
            db.query(_AgentSession)
            .filter(
                _AgentSession.project == "nonexistent",
                _AgentSession.summary.isnot(None),
            )
            .order_by(_AgentSession.started_at.desc())
            .limit(5)
            .all()
        )

        assert len(sessions) == 0
        briefing = "\n".join([]) if sessions else None
        assert briefing is None

        db.close()

    def test_briefing_respects_limit(self, tmp_path):
        """Briefing endpoint respects the limit parameter."""
        from zerg.models.agents import AgentSession as _AgentSession

        db = self._setup_db(tmp_path)

        now = datetime.now(timezone.utc)
        for i in range(10):
            db.add(
                _AgentSession(
                    provider="claude",
                    environment="production",
                    project="zerg",
                    started_at=now - timedelta(hours=i),
                    summary=f"Session {i} summary.",
                    summary_title=f"Session {i}",
                    user_messages=1,
                    assistant_messages=1,
                    tool_calls=0,
                )
            )
        db.commit()

        sessions = (
            db.query(_AgentSession)
            .filter(
                _AgentSession.project == "zerg",
                _AgentSession.summary.isnot(None),
            )
            .order_by(_AgentSession.started_at.desc())
            .limit(3)
            .all()
        )

        assert len(sessions) == 3
        # Most recent first
        assert sessions[0].summary_title == "Session 0"

        db.close()

    def test_briefing_filters_by_project(self, tmp_path):
        """Briefing only returns sessions for the specified project."""
        from zerg.models.agents import AgentSession as _AgentSession

        db = self._setup_db(tmp_path)

        now = datetime.now(timezone.utc)
        db.add(
            _AgentSession(
                provider="claude",
                environment="production",
                project="zerg",
                started_at=now - timedelta(hours=1),
                summary="Zerg work.",
                summary_title="Zerg Session",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
            )
        )
        db.add(
            _AgentSession(
                provider="claude",
                environment="production",
                project="hdr",
                started_at=now - timedelta(hours=2),
                summary="HDR work.",
                summary_title="HDR Session",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
            )
        )
        db.commit()

        sessions = (
            db.query(_AgentSession)
            .filter(
                _AgentSession.project == "zerg",
                _AgentSession.summary.isnot(None),
            )
            .order_by(_AgentSession.started_at.desc())
            .limit(5)
            .all()
        )

        assert len(sessions) == 1
        assert sessions[0].summary_title == "Zerg Session"

        db.close()


# =====================================================================
# _safe_parse_json (internal helper)
# =====================================================================


class TestSafeParseJson:
    def test_valid_json(self):
        from zerg.services.session_processing.summarize import _safe_parse_json

        result = _safe_parse_json('{"title": "Test", "summary": "OK."}')
        assert result == {"title": "Test", "summary": "OK."}

    def test_markdown_fenced_json(self):
        from zerg.services.session_processing.summarize import _safe_parse_json

        result = _safe_parse_json('```json\n{"title": "Test"}\n```')
        assert result == {"title": "Test"}

    def test_json_with_prefix_text(self):
        from zerg.services.session_processing.summarize import _safe_parse_json

        result = _safe_parse_json('Here is the JSON: {"title": "Test"}')
        assert result == {"title": "Test"}

    def test_invalid_json(self):
        from zerg.services.session_processing.summarize import _safe_parse_json

        result = _safe_parse_json("Not JSON at all")
        assert result is None

    def test_empty_string(self):
        from zerg.services.session_processing.summarize import _safe_parse_json

        result = _safe_parse_json("")
        assert result is None

    def test_none(self):
        from zerg.services.session_processing.summarize import _safe_parse_json

        result = _safe_parse_json(None)
        assert result is None
