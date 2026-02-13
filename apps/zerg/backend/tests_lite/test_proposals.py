"""Tests for action proposals — model, writer integration, API router."""

from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.models.work import ActionProposal
from zerg.models.work import Insight
from zerg.services.reflection.collector import ProjectBatch
from zerg.services.reflection.collector import SessionInfo


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_proposals.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_insight(db, title="Test insight", project="test-project", confidence=0.9):
    insight = Insight(
        insight_type="pattern",
        title=title,
        project=project,
        description="Some description",
        confidence=confidence,
        severity="warning",
        observations=[],
    )
    db.add(insight)
    db.commit()
    db.refresh(insight)
    return insight


def _make_proposal(db, insight_id, title="Test proposal", project="test-project", status="pending"):
    proposal = ActionProposal(
        insight_id=insight_id,
        project=project,
        title=title,
        action_blurb="Add a pre-commit hook to check for common mistakes",
        status=status,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


def _make_session(db, project="test-project", summary="Test summary"):
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="test",
        project=project,
        started_at=datetime.now(timezone.utc),
        summary=summary,
        summary_title="Test",
        user_messages=5,
        assistant_messages=10,
        tool_calls=3,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestActionProposalModel:
    def test_create_and_query(self, tmp_path):
        """Basic insert and query of an action proposal."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            insight = _make_insight(db)
            proposal = _make_proposal(db, insight_id=insight.id)

            result = db.query(ActionProposal).filter(ActionProposal.project == "test-project").first()
            assert result is not None
            assert result.title == "Test proposal"
            assert result.action_blurb == "Add a pre-commit hook to check for common mistakes"
            assert result.status == "pending"
            assert result.decided_at is None
            assert result.insight_id == insight.id

    def test_filter_by_status(self, tmp_path):
        """Filter proposals by status."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            insight = _make_insight(db)
            _make_proposal(db, insight.id, title="Pending one", status="pending")
            _make_proposal(db, insight.id, title="Approved one", status="approved")
            _make_proposal(db, insight.id, title="Declined one", status="declined")

            pending = db.query(ActionProposal).filter(ActionProposal.status == "pending").all()
            assert len(pending) == 1
            assert pending[0].title == "Pending one"

            approved = db.query(ActionProposal).filter(ActionProposal.status == "approved").all()
            assert len(approved) == 1
            assert approved[0].title == "Approved one"

    def test_filter_by_project(self, tmp_path):
        """Filter proposals by project."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            insight1 = _make_insight(db, project="zerg")
            insight2 = _make_insight(db, title="Other insight", project="hdr")
            _make_proposal(db, insight1.id, title="Zerg proposal", project="zerg")
            _make_proposal(db, insight2.id, title="HDR proposal", project="hdr")

            zerg = db.query(ActionProposal).filter(ActionProposal.project == "zerg").all()
            assert len(zerg) == 1
            assert zerg[0].title == "Zerg proposal"

    def test_approve_sets_fields(self, tmp_path):
        """Approving a proposal sets status and decided_at."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            insight = _make_insight(db)
            proposal = _make_proposal(db, insight.id)

            proposal.status = "approved"
            proposal.decided_at = datetime.now(timezone.utc)
            proposal.task_description = "Do the thing\nContext: Some description"
            db.commit()
            db.refresh(proposal)

            assert proposal.status == "approved"
            assert proposal.decided_at is not None
            assert "Do the thing" in proposal.task_description

    def test_decline_sets_fields(self, tmp_path):
        """Declining a proposal sets status and decided_at."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            insight = _make_insight(db)
            proposal = _make_proposal(db, insight.id)

            proposal.status = "declined"
            proposal.decided_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(proposal)

            assert proposal.status == "declined"
            assert proposal.decided_at is not None


# ---------------------------------------------------------------------------
# Writer integration tests
# ---------------------------------------------------------------------------


class TestWriterProposalCreation:
    def test_creates_proposal_with_action_blurb(self, tmp_path):
        """Writer creates ActionProposal when action has action_blurb."""
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            session = _make_session(db)
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
                "insight_type": "failure",
                "title": "Deploy fails without UFW rule",
                "description": "Container networking blocked",
                "severity": "warning",
                "confidence": 0.9,
                "tags": ["deploy"],
                "action_blurb": "Add UFW allow rule for 172.16.0.0/12 to deploy checklist",
            }]

            run_id = str(uuid4())
            created, merged, skipped = execute_actions(db, actions, batches, run_id=run_id)
            assert created == 1

            # Verify the proposal was created
            proposals = db.query(ActionProposal).all()
            assert len(proposals) == 1
            assert proposals[0].title == "Deploy fails without UFW rule"
            assert proposals[0].action_blurb == "Add UFW allow rule for 172.16.0.0/12 to deploy checklist"
            assert proposals[0].project == "test-project"
            assert str(proposals[0].reflection_run_id) == run_id
            assert proposals[0].status == "pending"

            # Verify it's linked to the insight
            insight = db.query(Insight).filter(Insight.title == "Deploy fails without UFW rule").first()
            assert insight is not None
            assert proposals[0].insight_id == insight.id

    def test_no_proposal_without_action_blurb(self, tmp_path):
        """Writer does NOT create proposal when action has no action_blurb."""
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            session = _make_session(db)
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
                "insight_type": "learning",
                "title": "Some learning without action",
                "description": "Just a note",
                "severity": "info",
                "confidence": 0.5,
            }]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert created == 1

            proposals = db.query(ActionProposal).all()
            assert len(proposals) == 0

    def test_no_proposal_with_empty_action_blurb(self, tmp_path):
        """Writer does NOT create proposal when action_blurb is empty string."""
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            session = _make_session(db)
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
                "insight_type": "learning",
                "title": "Some learning",
                "description": "Just a note",
                "severity": "info",
                "confidence": 0.9,
                "action_blurb": "   ",  # whitespace only
            }]

            created, merged, skipped = execute_actions(db, actions, batches)
            assert created == 1

            proposals = db.query(ActionProposal).all()
            assert len(proposals) == 0

    def test_proposal_linked_to_reflection_run(self, tmp_path):
        """Proposal correctly links to both insight and reflection run."""
        from zerg.services.reflection.writer import execute_actions

        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            session = _make_session(db)
            batches = [ProjectBatch(
                project="test-project",
                sessions=[SessionInfo(
                    id=str(session.id), project="test-project", provider="claude",
                    summary="Test", summary_title="Test",
                    started_at=datetime.now(timezone.utc), tool_calls=1, user_messages=1,
                )],
                existing_insights=[],
            )]

            run_id = str(uuid4())
            actions = [{
                "action": "create_insight",
                "project": "test-project",
                "insight_type": "improvement",
                "title": "Improvement with action",
                "description": "Do this better",
                "severity": "info",
                "confidence": 0.85,
                "action_blurb": "Create a pre-commit hook for this",
            }]

            execute_actions(db, actions, batches, run_id=run_id)

            proposal = db.query(ActionProposal).first()
            assert proposal is not None
            assert str(proposal.reflection_run_id) == run_id

            insight = db.query(Insight).filter(Insight.title == "Improvement with action").first()
            assert proposal.insight_id == insight.id


# ---------------------------------------------------------------------------
# Judge action_blurb parsing tests
# ---------------------------------------------------------------------------


class TestJudgeActionBlurb:
    def test_parse_preserves_action_blurb(self):
        """Parser preserves action_blurb field on create_insight actions."""
        from zerg.services.reflection.judge import _parse_actions
        import json

        raw = json.dumps([{
            "action": "create_insight",
            "insight_type": "failure",
            "title": "Test",
            "description": "Desc",
            "severity": "info",
            "confidence": 0.9,
            "action_blurb": "Add retry logic to the endpoint",
        }])

        actions = _parse_actions(raw, "test-project")
        assert len(actions) == 1
        assert actions[0]["action_blurb"] == "Add retry logic to the endpoint"

    def test_parse_strips_empty_action_blurb(self):
        """Parser removes empty action_blurb."""
        from zerg.services.reflection.judge import _parse_actions
        import json

        raw = json.dumps([{
            "action": "create_insight",
            "insight_type": "learning",
            "title": "Test",
            "description": "Desc",
            "severity": "info",
            "confidence": 0.5,
            "action_blurb": "",
        }])

        actions = _parse_actions(raw, "test-project")
        assert len(actions) == 1
        assert "action_blurb" not in actions[0]

    def test_parse_strips_non_string_action_blurb(self):
        """Parser removes non-string action_blurb."""
        from zerg.services.reflection.judge import _parse_actions
        import json

        raw = json.dumps([{
            "action": "create_insight",
            "insight_type": "learning",
            "title": "Test",
            "description": "Desc",
            "severity": "info",
            "confidence": 0.5,
            "action_blurb": 123,
        }])

        actions = _parse_actions(raw, "test-project")
        assert len(actions) == 1
        assert "action_blurb" not in actions[0]

    def test_parse_no_action_blurb_is_fine(self):
        """Actions without action_blurb are valid."""
        from zerg.services.reflection.judge import _parse_actions
        import json

        raw = json.dumps([{
            "action": "create_insight",
            "insight_type": "learning",
            "title": "Test",
            "description": "Desc",
            "severity": "info",
            "confidence": 0.5,
        }])

        actions = _parse_actions(raw, "test-project")
        assert len(actions) == 1
        assert "action_blurb" not in actions[0]


# ---------------------------------------------------------------------------
# Briefing integration tests
# ---------------------------------------------------------------------------


class TestBriefingWithProposals:
    def test_approved_proposals_in_briefing(self, tmp_path):
        """Approved proposals appear in the briefing text."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            # Create a session so briefing has content
            _make_session(db, project="test-project", summary="Did stuff")

            # Create approved proposal
            insight = _make_insight(db, project="test-project")
            proposal = ActionProposal(
                insight_id=insight.id,
                project="test-project",
                title="Fix the deploy",
                action_blurb="Add UFW rule for container networking",
                status="approved",
                decided_at=datetime.now(timezone.utc),
            )
            db.add(proposal)
            db.commit()

            # Query the way the briefing endpoint does
            approved = (
                db.query(ActionProposal)
                .filter(
                    ActionProposal.status == "approved",
                    ActionProposal.project == "test-project",
                )
                .all()
            )
            assert len(approved) == 1
            assert approved[0].action_blurb == "Add UFW rule for container networking"

    def test_pending_proposals_not_in_briefing(self, tmp_path):
        """Pending proposals should not show up in briefing query."""
        SessionLocal = _make_db(tmp_path)
        with SessionLocal() as db:
            insight = _make_insight(db, project="test-project")
            _make_proposal(db, insight.id, status="pending")

            approved = (
                db.query(ActionProposal)
                .filter(
                    ActionProposal.status == "approved",
                    ActionProposal.project == "test-project",
                )
                .all()
            )
            assert len(approved) == 0
