"""Tests for Oikos run history endpoint."""

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models import CommisJob
from zerg.models import Fiche
from zerg.models import Run
from zerg.models import Thread
from zerg.models import User
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurnReview
from zerg.models.enums import FicheStatus
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.enums import ThreadType
from zerg.models.enums import UserRole
from zerg.services.session_loop_controller import LoopControllerDecision
from zerg.services.session_turn_reviews import maybe_process_session_turn_loop


def _make_db(tmp_path):
    """Create a SQLite DB for tests and return a session factory."""
    db_path = tmp_path / "test_oikos_runs.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    """Create TestClient with DB + Oikos auth dependency overrides."""
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user

    return TestClient(app, backend="asyncio"), api_app


def test_oikos_runs_are_automation_first_and_support_automation_filter(tmp_path):
    """GET /api/oikos/runs returns automation-first summaries and filters by automation_id."""
    session_local = _make_db(tmp_path)
    base_time = datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc)

    with session_local() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        other = User(email="other@local", role=UserRole.USER.value)
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        owner_primary = Fiche(
            owner_id=owner.id,
            name="Priority Inbox",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        owner_secondary = Fiche(
            owner_id=owner.id,
            name="Background Sweep",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        other_fiche = Fiche(
            owner_id=other.id,
            name="Other Owner",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        db.add_all([owner_primary, owner_secondary, other_fiche])
        db.commit()
        db.refresh(owner_primary)
        db.refresh(owner_secondary)
        db.refresh(other_fiche)

        owner_primary_thread = Thread(
            fiche_id=owner_primary.id,
            title="Priority Thread",
            thread_type=ThreadType.MANUAL.value,
        )
        owner_secondary_thread = Thread(
            fiche_id=owner_secondary.id,
            title="Background Thread",
            thread_type=ThreadType.MANUAL.value,
        )
        other_thread = Thread(
            fiche_id=other_fiche.id,
            title="Other Thread",
            thread_type=ThreadType.MANUAL.value,
        )
        db.add_all([owner_primary_thread, owner_secondary_thread, other_thread])
        db.commit()
        db.refresh(owner_primary_thread)
        db.refresh(owner_secondary_thread)
        db.refresh(other_thread)

        owner_primary_run = Run(
            fiche_id=owner_primary.id,
            thread_id=owner_primary_thread.id,
            status=RunStatus.RUNNING.value,
            trigger=RunTrigger.MANUAL.value,
            summary="Need your input",
            created_at=base_time + timedelta(minutes=2),
            updated_at=base_time + timedelta(minutes=2),
        )
        owner_secondary_run = Run(
            fiche_id=owner_secondary.id,
            thread_id=owner_secondary_thread.id,
            status=RunStatus.SUCCESS.value,
            trigger=RunTrigger.SCHEDULE.value,
            summary="Wrapped up cleanly",
            created_at=base_time + timedelta(minutes=1),
            updated_at=base_time + timedelta(minutes=1),
        )
        other_run = Run(
            fiche_id=other_fiche.id,
            thread_id=other_thread.id,
            status=RunStatus.FAILED.value,
            trigger=RunTrigger.MANUAL.value,
            error="Should not leak",
            created_at=base_time,
            updated_at=base_time,
        )
        db.add_all([owner_primary_run, owner_secondary_run, other_run])
        db.commit()
        db.refresh(owner_primary_run)
        db.refresh(owner_secondary_run)

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.get("/api/oikos/runs?limit=10")
            assert response.status_code == 200, response.text
            payload = response.json()

            assert [row["id"] for row in payload] == [owner_primary_run.id, owner_secondary_run.id]
            assert payload[0]["automation_id"] == owner_primary.id
            assert payload[0]["automation_name"] == "Priority Inbox"
            assert "task_id" not in payload[0]
            assert "task_name" not in payload[0]
            assert "fiche_id" not in payload[0]
            assert "fiche_name" not in payload[0]
            assert payload[0]["signal"] == "Need your input"
            assert payload[1]["automation_id"] == owner_secondary.id
            assert payload[1]["automation_name"] == "Background Sweep"

            automation_filtered = client.get(f"/api/oikos/runs?automation_id={owner_secondary.id}")
            assert automation_filtered.status_code == 200, automation_filtered.text
            assert [row["id"] for row in automation_filtered.json()] == [owner_secondary_run.id]
        finally:
            api_app_ref.dependency_overrides = {}


def _create_session(
    db,
    *,
    loop_mode: str = "assist",
    summary_title: str | None = None,
    project: str = "zerg",
    device_id: str | None = "cinder",
    provider: str = "claude",
):
    started_at = datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc)
    session = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="development",
        project=project,
        device_id=device_id,
        cwd=f"/tmp/{project}",
        started_at=started_at,
        ended_at=started_at,
        summary_title=summary_title,
        loop_mode=loop_mode,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_turn(db, *, session_id, user_text: str, assistant_text: str):
    timestamp = datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc)
    user_event = AgentEvent(
        session_id=session_id,
        role="user",
        content_text=user_text,
        timestamp=timestamp,
    )
    assistant_event = AgentEvent(
        session_id=session_id,
        role="assistant",
        content_text=assistant_text,
        timestamp=timestamp + timedelta(seconds=1),
    )
    db.add_all([user_event, assistant_event])
    db.commit()
    db.refresh(user_event)
    db.refresh(assistant_event)
    return user_event, assistant_event


def _add_review(
    db,
    *,
    owner_id: int,
    session: AgentSession,
    assistant_event_id: int,
    created_at: datetime,
    execution_state: str,
    decision: str = "continue",
    summary: str = "Run the pending targeted tests.",
    rationale: str = "The session has one obvious next step.",
    recommended_action: str | None = "continue_session",
    follow_up_prompt: str | None = "Run the pending targeted tests.",
    status: str = "enqueued",
    reason: str | None = "notify_user",
):
    review = SessionTurnReview(
        session_id=session.id,
        owner_id=owner_id,
        assistant_event_id=assistant_event_id,
        turn_index=1,
        trigger_type="turn.completed",
        loop_mode=session.loop_mode,
        decision=decision,
        summary=summary,
        rationale=rationale,
        turn_excerpt="Only targeted verification remains.",
        mode_capability="notify_only",
        mode_summary="Suggest or escalate from completed turns, but wait for user approval before continuing.",
        execution_state=execution_state,
        recommended_action=recommended_action,
        follow_up_prompt=follow_up_prompt,
        blocked_reasons=[],
        status=status,
        reason=reason,
        created_at=created_at,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def test_loop_inbox_returns_latest_attention_reviews_only(tmp_path):
    session_local = _make_db(tmp_path)
    base_time = datetime(2026, 3, 18, 9, 0, tzinfo=timezone.utc)

    with session_local() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        other = User(email="other@local", role=UserRole.USER.value)
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        session_a = _create_session(
            db,
            loop_mode="assist",
            summary_title="Auth Refresh",
            project="zerg",
            device_id="cinder",
        )
        _, assistant_a = _add_turn(
            db,
            session_id=session_a.id,
            user_text="finish auth refresh",
            assistant_text="Run the pending targeted tests next.",
        )
        _add_review(
            db,
            owner_id=owner.id,
            session=session_a,
            assistant_event_id=assistant_a.id,
            created_at=base_time + timedelta(minutes=1),
            execution_state="awaiting_user_approval",
        )

        session_b = _create_session(
            db,
            loop_mode="assist",
            summary_title="Infra Decision",
            project="sauron",
            device_id="clifford",
        )
        _, assistant_b = _add_turn(
            db,
            session_id=session_b.id,
            user_text="investigate ci runner issue",
            assistant_text="This needs a product decision about rollout order.",
        )
        _add_review(
            db,
            owner_id=owner.id,
            session=session_b,
            assistant_event_id=assistant_b.id,
            created_at=base_time + timedelta(minutes=2),
            execution_state="needs_human",
            decision="escalate",
            summary="Choose the rollout order before continuing.",
            rationale="The turn requires a human priority call.",
            recommended_action="escalate",
            follow_up_prompt=None,
        )

        session_c = _create_session(
            db,
            loop_mode="assist",
            summary_title="Quiet Session",
            project="hdr",
            device_id="cube",
        )
        _, assistant_c = _add_turn(
            db,
            session_id=session_c.id,
            user_text="finish upload fix",
            assistant_text="Looks done.",
        )
        _add_review(
            db,
            owner_id=owner.id,
            session=session_c,
            assistant_event_id=assistant_c.id,
            created_at=base_time,
            execution_state="awaiting_user_approval",
        )
        _, assistant_c_done = _add_turn(
            db,
            session_id=session_c.id,
            user_text="confirm if anything else remains",
            assistant_text="Looks done.",
        )
        _add_review(
            db,
            owner_id=owner.id,
            session=session_c,
            assistant_event_id=assistant_c_done.id,
            created_at=base_time + timedelta(minutes=3),
            execution_state="no_action",
            decision="done",
            summary="No meaningful follow-up is needed.",
            rationale="The task appears complete.",
            recommended_action="done",
            follow_up_prompt=None,
            status="recorded",
            reason=None,
        )

        other_session = _create_session(db, summary_title="Other Owner", project="private")
        _, assistant_other = _add_turn(
            db,
            session_id=other_session.id,
            user_text="do private thing",
            assistant_text="Continue the same session.",
        )
        _add_review(
            db,
            owner_id=other.id,
            session=other_session,
            assistant_event_id=assistant_other.id,
            created_at=base_time + timedelta(minutes=4),
            execution_state="awaiting_user_approval",
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.get("/api/oikos/loop-inbox?limit=10")
            assert response.status_code == 200, response.text
            payload = response.json()

            assert [row["title"] for row in payload] == ["Infra Decision", "Auth Refresh"]
            assert [row["decision"] for row in payload] == ["escalate", "continue"]
            assert payload[0]["requires_attention"] is True
            assert payload[0]["machine"] == "clifford"
            assert payload[1]["project"] == "zerg"
            assert payload[1]["recommended_action"] == "continue_session"
        finally:
            api_app_ref.dependency_overrides = {}


def test_loop_inbox_action_card_returns_compact_turn_context(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        db.add(owner)
        db.commit()
        db.refresh(owner)

        session = _create_session(
            db,
            loop_mode="assist",
            summary_title="Session Detail Page",
            project="zerg",
            device_id="cinder",
        )
        user_event, assistant_event = _add_turn(
            db,
            session_id=session.id,
            user_text="finish the session detail page",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )
        _add_review(
            db,
            owner_id=owner.id,
            session=session,
            assistant_event_id=assistant_event.id,
            created_at=datetime(2026, 3, 18, 9, 5, tzinfo=timezone.utc),
            execution_state="awaiting_user_approval",
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.get(f"/api/oikos/loop-inbox/{session.id}")
            assert response.status_code == 200, response.text
            payload = response.json()

            assert payload["session_id"] == str(session.id)
            assert payload["title"] == "Session Detail Page"
            assert payload["last_user_text"] == "finish the session detail page"
            assert (
                payload["last_assistant_text"]
                == "Only targeted verification remains. Run the pending targeted tests."
            )
            assert payload["mode_capability"] == "notify_only"
            assert payload["available_actions"] == [
                "approve_recommended_action",
                "not_now",
                "open_full_session",
            ]
            assert payload["follow_up_prompt"] == "Run the pending targeted tests."
            assert payload["requires_attention"] is True
            assert user_event.id < assistant_event.id
        finally:
            api_app_ref.dependency_overrides = {}


def test_loop_inbox_action_card_404s_when_latest_review_no_longer_needs_attention(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        db.add(owner)
        db.commit()
        db.refresh(owner)

        session = _create_session(db, loop_mode="assist", summary_title="Done Session")
        _, assistant_event = _add_turn(
            db,
            session_id=session.id,
            user_text="finish it",
            assistant_text="Everything is done.",
        )
        _add_review(
            db,
            owner_id=owner.id,
            session=session,
            assistant_event_id=assistant_event.id,
            created_at=datetime(2026, 3, 18, 9, 10, tzinfo=timezone.utc),
            execution_state="no_action",
            decision="done",
            summary="No meaningful follow-up is needed.",
            rationale="The task appears complete.",
            recommended_action="done",
            follow_up_prompt=None,
            status="recorded",
            reason=None,
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.get(f"/api/oikos/loop-inbox/{session.id}")
            assert response.status_code == 404
        finally:
            api_app_ref.dependency_overrides = {}


def test_loop_inbox_approve_action_queues_same_session_continue_and_clears_item(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(
            email="owner@local",
            role=UserRole.USER.value,
            context={
                "preferences": {
                    "operator_mode": {
                        "enabled": True,
                        "allow_continue": True,
                        "allow_notify": True,
                    }
                }
            },
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        session = _create_session(
            db,
            loop_mode="assist",
            summary_title="Targeted Tests",
            project="zerg",
            device_id="cinder",
        )
        _, assistant_event = _add_turn(
            db,
            session_id=session.id,
            user_text="finish targeted verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )
        review = _add_review(
            db,
            owner_id=owner.id,
            session=session,
            assistant_event_id=assistant_event.id,
            created_at=datetime(2026, 3, 18, 9, 15, tzinfo=timezone.utc),
            execution_state="awaiting_user_approval",
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.post(
                f"/api/oikos/loop-inbox/{session.id}/actions",
                json={"action": "approve_recommended_action"},
            )
            assert response.status_code == 200, response.text
            payload = response.json()

            assert payload["session_id"] == str(session.id)
            assert payload["review_id"] == review.id
            assert payload["action"] == "approve_recommended_action"
            assert payload["status"] == "acted"
            assert payload["reason"] == "continue_session"
            assert payload["queued_job_id"] is not None

            queued_jobs = db.query(CommisJob).all()
            assert len(queued_jobs) == 1
            assert queued_jobs[0].task == "Run the pending targeted tests."
            assert queued_jobs[0].config["resume_session_id"] == str(session.id)

            review_row = db.query(SessionTurnReview).filter(SessionTurnReview.id == review.id).one()
            assert review_row.actual_outcome == "continue_session"
            assert review_row.status == "acted"

            inbox_after = client.get("/api/oikos/loop-inbox")
            assert inbox_after.status_code == 200
            assert inbox_after.json() == []
        finally:
            api_app_ref.dependency_overrides = {}


def test_loop_inbox_not_now_action_hides_pending_item(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        db.add(owner)
        db.commit()
        db.refresh(owner)

        session = _create_session(db, loop_mode="assist", summary_title="Pause This")
        _, assistant_event = _add_turn(
            db,
            session_id=session.id,
            user_text="check if we should continue",
            assistant_text="This needs a quick product decision.",
        )
        review = _add_review(
            db,
            owner_id=owner.id,
            session=session,
            assistant_event_id=assistant_event.id,
            created_at=datetime(2026, 3, 18, 9, 20, tzinfo=timezone.utc),
            execution_state="needs_human",
            decision="escalate",
            summary="Choose whether to ship now or later.",
            rationale="The turn needs a real decision.",
            recommended_action="escalate",
            follow_up_prompt=None,
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.post(
                f"/api/oikos/loop-inbox/{session.id}/actions",
                json={"action": "not_now"},
            )
            assert response.status_code == 200, response.text
            payload = response.json()

            assert payload["review_id"] == review.id
            assert payload["status"] == "acted"
            assert payload["reason"] == "not_now"
            assert payload["queued_job_id"] is None

            review_row = db.query(SessionTurnReview).filter(SessionTurnReview.id == review.id).one()
            assert review_row.actual_outcome == "ignore"
            assert review_row.status == "acted"

            detail_after = client.get(f"/api/oikos/loop-inbox/{session.id}")
            assert detail_after.status_code == 404
        finally:
            api_app_ref.dependency_overrides = {}


def test_loop_inbox_end_to_end_phone_approve_flow(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Only targeted verification remains.",
            rationale="The assistant left one obvious bounded next step.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=41,
        )

    async def _fake_invoke(*_args, **_kwargs):
        return 777

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.oikos_service.invoke_oikos", _fake_invoke)

    with session_local() as db:
        owner = User(
            email="owner@local",
            role=UserRole.USER.value,
            context={
                "preferences": {
                    "operator_mode": {
                        "enabled": True,
                        "allow_continue": True,
                        "allow_notify": True,
                    }
                }
            },
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        session = _create_session(
            db,
            loop_mode="assist",
            summary_title="Phone Approve Flow",
            project="zerg",
            device_id="cinder",
        )
        fresh_time = datetime.now(timezone.utc)
        session.started_at = fresh_time
        session.ended_at = fresh_time
        db.commit()
        db.refresh(session)
        _add_turn(
            db,
            session_id=session.id,
            user_text="finish targeted verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        asyncio.run(maybe_process_session_turn_loop(db=db, session_id=str(session.id)))

        client, api_app_ref = _make_client(db, owner)
        try:
            inbox_before = client.get("/api/oikos/loop-inbox")
            assert inbox_before.status_code == 200, inbox_before.text
            inbox_payload = inbox_before.json()
            assert len(inbox_payload) == 1
            assert inbox_payload[0]["session_id"] == str(session.id)
            assert inbox_payload[0]["decision"] == "continue"
            assert inbox_payload[0]["recommended_action"] == "continue_session"

            card_response = client.get(f"/api/oikos/loop-inbox/{session.id}")
            assert card_response.status_code == 200, card_response.text
            card_payload = card_response.json()
            assert card_payload["follow_up_prompt"] == "Run the pending targeted tests."
            assert card_payload["available_actions"] == [
                "approve_recommended_action",
                "not_now",
                "open_full_session",
            ]

            action_response = client.post(
                f"/api/oikos/loop-inbox/{session.id}/actions",
                json={"action": "approve_recommended_action"},
            )
            assert action_response.status_code == 200, action_response.text
            action_payload = action_response.json()
            assert action_payload["status"] == "acted"
            assert action_payload["reason"] == "continue_session"
            assert action_payload["queued_job_id"] is not None

            queued_jobs = db.query(CommisJob).all()
            assert len(queued_jobs) == 1
            assert queued_jobs[0].task == "Run the pending targeted tests."
            assert queued_jobs[0].config["resume_session_id"] == str(session.id)

            review_row = db.query(SessionTurnReview).filter(SessionTurnReview.session_id == session.id).one()
            assert review_row.execution_state == "awaiting_user_approval"
            assert review_row.status == "acted"
            assert review_row.actual_outcome == "continue_session"

            inbox_after = client.get("/api/oikos/loop-inbox")
            assert inbox_after.status_code == 200, inbox_after.text
            assert inbox_after.json() == []
        finally:
            api_app_ref.dependency_overrides = {}
