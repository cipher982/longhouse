"""Tests for the reflection service — collector, judge, writer, full flow."""

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.models.work import Insight
from zerg.models.work import ReflectionRun
from zerg.services.reflection.collector import ProjectBatch
from zerg.services.reflection.collector import SessionInfo
from zerg.services.reflection.collector import collect_sessions


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_reflection.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_session(
    db,
    project="test-project",
    summary="Did some work",
    summary_title="Test session",
    reflected_at=None,
    started_at=None,
):
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
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_insight(db, title="Known gotcha", project="test-project", insight_type="learning"):
    insight = Insight(
        insight_type=insight_type,
        title=title,
        project=project,
        description="Some description",
        confidence=0.8,
        observations=[],
    )
    db.add(insight)
    db.commit()
    db.refresh(insight)
    return insight


# ---------------------------------------------------------------------------
# Collector tests
# ---------------------------------------------------------------------------


class TestCollector:
    def test_collects_unreflected_sessions_with_summaries(self, tmp_path):
        """Only sessions with summary and reflected_at IS NULL are collected."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, summary="Has summary", reflected_at=None)
            _make_session(db, summary=None, reflected_at=None)
            _make_session(db, summary="Already reflected", reflected_at=datetime.now(timezone.utc))

            batches = collect_sessions(db, project="test-project", window_hours=24)
            assert len(batches) == 1
            assert len(batches[0].sessions) == 1
            assert batches[0].sessions[0].summary == "Has summary"

    def test_respects_window_hours(self, tmp_path):
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, summary="Recent", started_at=datetime.now(timezone.utc) - timedelta(hours=1))
            _make_session(db, summary="Old", started_at=datetime.now(timezone.utc) - timedelta(hours=48))

            batches = collect_sessions(db, project="test-project", window_hours=24)
            assert len(batches) == 1
            assert len(batches[0].sessions) == 1
            assert batches[0].sessions[0].summary == "Recent"

    def test_groups_by_project(self, tmp_path):
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="alpha", summary="Alpha session")
            _make_session(db, project="beta", summary="Beta session")

            batches = collect_sessions(db, window_hours=24)
            assert len(batches) == 2
            projects = {b.project for b in batches}
            assert projects == {"alpha", "beta"}

    def test_includes_existing_insights_for_dedup(self, tmp_path):
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="test-project", summary="Test session")
            _make_insight(db, title="Known gotcha", project="test-project")

            batches = collect_sessions(db, project="test-project", window_hours=24)
            assert len(batches) == 1
            assert len(batches[0].existing_insights) == 1
            assert batches[0].existing_insights[0]["title"] == "Known gotcha"

    def test_empty_when_no_sessions(self, tmp_path):
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            batches = collect_sessions(db, project="test-project", window_hours=24)
            assert batches == []

    def test_skips_sessions_without_summaries(self, tmp_path):
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, summary=None)
            batches = collect_sessions(db, project="test-project", window_hours=24)
            assert batches == []


# ---------------------------------------------------------------------------
# Judge tests
# ---------------------------------------------------------------------------


class TestJudge:
    @pytest.mark.asyncio
    async def test_produces_valid_actions_from_llm(self):
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
            {"action": "skip", "reason": "One-off debugging session"},
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
        assert usage["prompt_tokens"] == 100

    @pytest.mark.asyncio
    async def test_handles_empty_sessions(self):
        from zerg.services.reflection.judge import analyze_sessions

        batch = ProjectBatch(project="test", sessions=[], existing_insights=[])
        actions, usage = await analyze_sessions(batch, llm_client=None, model="test")
        assert actions == []

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self):
        from zerg.services.reflection.judge import analyze_sessions

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "not valid json"
        mock_response.usage = MagicMock(prompt_tokens=50, completion_tokens=10)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        batch = ProjectBatch(
            project="test",
            sessions=[SessionInfo(
                id="s1", project="test", provider="claude",
                summary="Test", summary_title="Test",
                started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
            )],
            existing_insights=[],
        )
        actions, _ = await analyze_sessions(batch, llm_client=mock_client, model="test")
        assert actions == []

    @pytest.mark.asyncio
    async def test_handles_no_llm_client(self):
        from zerg.services.reflection.judge import analyze_sessions

        batch = ProjectBatch(
            project="test",
            sessions=[SessionInfo(
                id="s1", project="test", provider="claude",
                summary="Test", summary_title="Test",
                started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
            )],
            existing_insights=[],
        )
        actions, usage = await analyze_sessions(batch, llm_client=None, model="test")
        assert actions == []
        assert usage["prompt_tokens"] == 0

    def test_parse_wrapper_object(self):
        from zerg.services.reflection.judge import _parse_actions

        raw = json.dumps({"actions": [{"action": "skip", "reason": "trivial"}]})
        actions = _parse_actions(raw, "test")
        assert len(actions) == 1
        assert actions[0]["action"] == "skip"

    def test_filters_invalid_actions(self):
        from zerg.services.reflection.judge import _parse_actions

        raw = json.dumps([
            {"action": "create_insight", "title": "Valid"},
            {"action": "invalid_action"},
            {"not_an_action": True},
        ])
        actions = _parse_actions(raw, "test")
        assert len(actions) == 1


# ---------------------------------------------------------------------------
# Writer tests
# ---------------------------------------------------------------------------


class TestWriter:
    def test_creates_insight(self, tmp_path):
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            session = _make_session(db, project="test-project", summary="Test")
            batches = [ProjectBatch(
                project="test-project",
                sessions=[SessionInfo(
                    id=str(session.id), project="test-project", provider="claude",
                    summary="Test", summary_title="Test",
                    started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
                )],
                existing_insights=[],
            )]

            actions = [{
                "action": "create_insight",
                "project": "test-project",
                "insight_type": "pattern",
                "title": "New insight from reflection",
                "description": "Learned something important",
                "severity": "info",
                "confidence": 0.9,
                "tags": ["test"],
            }]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert created == 1
            assert merged == 0

            insight = db.query(Insight).filter(Insight.title == "New insight from reflection").first()
            assert insight is not None
            assert insight.confidence == 0.9

    def test_merges_into_existing(self, tmp_path):
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            existing = _make_insight(db, title="Known pattern", project="test-project")
            batches = [ProjectBatch(project="test-project", sessions=[], existing_insights=[])]

            actions = [{
                "action": "merge",
                "insight_id": str(existing.id),
                "observation": "Seen again in another session",
            }]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert merged == 1

            db.refresh(existing)
            assert len(existing.observations) == 1
            assert "Seen again" in existing.observations[0]

    def test_stamps_reflected_at(self, tmp_path):
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            session = _make_session(db, project="test-project", summary="Test")
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

            execute_actions(db, [], batches)

            db.refresh(session)
            assert session.reflected_at is not None

    def test_dedup_prevents_duplicate(self, tmp_path):
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_insight(db, title="Duplicate title", project="test-project")
            batches = [ProjectBatch(project="test-project", sessions=[], existing_insights=[])]

            actions = [{
                "action": "create_insight",
                "project": "test-project",
                "insight_type": "learning",
                "title": "Duplicate title",
                "description": "New observation",
            }]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert created == 0
            assert merged == 1

    def test_cross_project_dedup(self, tmp_path):
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_insight(db, title="Cross-project bug", project="alpha")
            batches = [ProjectBatch(project="beta", sessions=[], existing_insights=[])]

            actions = [{
                "action": "create_insight",
                "project": "beta",
                "insight_type": "failure",
                "title": "Cross-project bug",
                "description": "Same bug in beta",
            }]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert created == 0
            assert merged == 1

            existing = db.query(Insight).filter(Insight.title == "Cross-project bug").first()
            assert "beta" in (existing.tags or [])

    def test_merge_nonexistent_skips(self, tmp_path):
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            batches = [ProjectBatch(project="test", sessions=[], existing_insights=[])]
            actions = [{"action": "merge", "insight_id": str(uuid4()), "observation": "x"}]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert skipped == 1

    def test_skip_counted(self, tmp_path):
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            batches = [ProjectBatch(project="test", sessions=[], existing_insights=[])]
            actions = [{"action": "skip"}, {"action": "skip"}]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert skipped == 2


# ---------------------------------------------------------------------------
# Full flow tests
# ---------------------------------------------------------------------------


class TestFullFlow:
    @pytest.mark.asyncio
    async def test_full_reflection_flow(self, tmp_path):
        """Ingest sessions → reflect → insights created → sessions stamped."""
        from zerg.services.reflection import reflect

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            s1 = _make_session(db, project="myapp", summary="Fixed auth bug")
            s2 = _make_session(db, project="myapp", summary="Deployed monitoring")

            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = json.dumps([
                {
                    "action": "create_insight",
                    "insight_type": "pattern",
                    "title": "Auth needs monitoring",
                    "description": "Multiple sessions dealt with auth",
                    "severity": "warning",
                    "confidence": 0.85,
                },
                {"action": "skip", "reason": "Routine"},
            ])
            mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            result = await reflect(db=db, project="myapp", window_hours=24,
                                   llm_client=mock_client, model="test-model")

            assert result.session_count == 2
            assert result.insights_created == 1
            assert result.insights_skipped == 1
            assert result.error is None

            insight = db.query(Insight).filter(Insight.title == "Auth needs monitoring").first()
            assert insight is not None

            db.refresh(s1)
            db.refresh(s2)
            assert s1.reflected_at is not None
            assert s2.reflected_at is not None

            run = db.query(ReflectionRun).first()
            assert run.status == "completed"

    @pytest.mark.asyncio
    async def test_idempotency(self, tmp_path):
        """Running reflection twice doesn't re-process stamped sessions."""
        from zerg.services.reflection import reflect

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="myapp", summary="Test session")

            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = json.dumps([
                {"action": "create_insight", "insight_type": "learning",
                 "title": "First run", "description": "Found first"},
            ])
            mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
            client1 = AsyncMock()
            client1.chat.completions.create = AsyncMock(return_value=mock_response)

            r1 = await reflect(db=db, project="myapp", window_hours=24,
                               llm_client=client1, model="test")
            assert r1.session_count == 1
            assert r1.insights_created == 1

            # Second run — sessions already stamped
            r2 = await reflect(db=db, project="myapp", window_hours=24,
                               llm_client=client1, model="test")
            assert r2.session_count == 0
            assert r2.insights_created == 0

    @pytest.mark.asyncio
    async def test_empty_case(self, tmp_path):
        """Reflection with no sessions completes cleanly."""
        from zerg.services.reflection import reflect

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            result = await reflect(db=db, project="myapp", window_hours=24,
                                   llm_client=None, model="test")
            assert result.session_count == 0
            assert result.error is None

            run = db.query(ReflectionRun).first()
            assert run.status == "completed"

    def test_reflection_run_model(self, tmp_path):
        """ReflectionRun model persists correctly."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            run = ReflectionRun(
                project="test",
                window_hours=24,
                model="gpt-5-mini",
                session_count=3,
                insights_created=1,
                insights_merged=1,
                insights_skipped=1,
                status="completed",
                completed_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.commit()
            db.refresh(run)

            assert run.id is not None
            assert run.status == "completed"
            assert run.session_count == 3


# ---------------------------------------------------------------------------
# Briefing insight integration tests
# ---------------------------------------------------------------------------


class TestBriefingInsights:
    """Tests for insights appearing in the briefing endpoint output."""

    def _build_briefing(self, db, project="myapp"):
        """Build briefing text using the same logic as the endpoint."""
        from zerg.models.work import INSIGHT_DEDUP_WINDOW_DAYS

        sessions = (
            db.query(AgentSession)
            .filter(AgentSession.project == project, AgentSession.summary.isnot(None))
            .order_by(AgentSession.started_at.desc())
            .limit(5)
            .all()
        )

        briefing_lines = []
        for s in sessions:
            title = s.summary_title or "Untitled"
            summary = s.summary or ""
            briefing_lines.append(f"- {title} -- {summary}")

        # Fetch insights (same logic as agents.py briefing)
        insight_cutoff = datetime.now(timezone.utc) - timedelta(days=INSIGHT_DEDUP_WINDOW_DAYS)
        insight_lines = []

        project_insights = (
            db.query(Insight)
            .filter(Insight.project == project, Insight.created_at >= insight_cutoff)
            .order_by(Insight.created_at.desc())
            .limit(5)
            .all()
        )
        cross_insights = (
            db.query(Insight)
            .filter(Insight.project != project, Insight.confidence >= 0.9, Insight.created_at >= insight_cutoff)
            .order_by(Insight.created_at.desc())
            .limit(3)
            .all()
        )

        seen_titles = set()
        for i in project_insights:
            if i.title not in seen_titles:
                severity_icon = {"critical": "!!!", "warning": "!!"}.get(i.severity, "")
                prefix = f"{severity_icon} " if severity_icon else ""
                desc = i.description or ""
                insight_lines.append(f"- {prefix}{i.title}" + (f": {desc}" if desc else ""))
                seen_titles.add(i.title)

        for i in cross_insights:
            if i.title not in seen_titles:
                source = i.project or "global"
                desc = i.description or ""
                insight_lines.append(f"- [from {source}] {i.title}" + (f": {desc}" if desc else ""))
                seen_titles.add(i.title)

        parts = []
        if briefing_lines:
            parts.extend(briefing_lines)
        if insight_lines:
            parts.append("")
            parts.append("Known gotchas:")
            parts.extend(insight_lines)

        return "\n".join(parts) if parts else None

    def test_briefing_includes_project_insights(self, tmp_path):
        """Project-specific insights appear in briefing."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="myapp", summary="Test session")
            _make_insight(db, title="Docker needs host networking", project="myapp")

            briefing = self._build_briefing(db, project="myapp")
            assert briefing is not None
            assert "Known gotchas" in briefing
            assert "Docker needs host networking" in briefing

    def test_briefing_includes_cross_project_high_confidence(self, tmp_path):
        """High-confidence insights from other projects appear in briefing."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="myapp", summary="Test session")
            insight = Insight(
                insight_type="failure",
                title="UFW blocks Docker traffic",
                description="Add 172.16.0.0/12",
                project="other-project",
                severity="critical",
                confidence=0.95,
                observations=[],
            )
            db.add(insight)
            db.commit()

            briefing = self._build_briefing(db, project="myapp")
            assert "UFW blocks Docker traffic" in briefing
            assert "from other-project" in briefing

    def test_briefing_no_insights_no_gotchas(self, tmp_path):
        """Briefing without insights has no gotchas section."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="myapp", summary="Test session")

            briefing = self._build_briefing(db, project="myapp")
            assert briefing is not None
            assert "Known gotchas" not in briefing

    def test_briefing_severity_icons(self, tmp_path):
        """Warning and critical insights get severity icons."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="myapp", summary="Test session")
            insight = Insight(
                insight_type="failure",
                title="Critical issue",
                project="myapp",
                severity="critical",
                observations=[],
            )
            db.add(insight)
            db.commit()

            briefing = self._build_briefing(db, project="myapp")
            assert "!!! Critical issue" in briefing

    def test_briefing_dedup_titles(self, tmp_path):
        """Same title from project and cross-project doesn't appear twice."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            _make_session(db, project="myapp", summary="Test session")
            _make_insight(db, title="Shared issue", project="myapp")
            insight = Insight(
                insight_type="learning",
                title="Shared issue",
                project="other",
                confidence=0.95,
                observations=[],
            )
            db.add(insight)
            db.commit()

            briefing = self._build_briefing(db, project="myapp")
            # Should appear only once
            assert briefing.count("Shared issue") == 1
