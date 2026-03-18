from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionTurnReview
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.services.session_loop_controller import LoopControllerDecision
from zerg.services.session_turn_reviews import maybe_record_session_turn_review


def _make_db(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_user(db, *, allow_continue: bool = False) -> User:
    user = User(
        email=f"user-{uuid4()}@example.com",
        role=UserRole.USER.value,
        context={
            "preferences": {
                "operator_mode": {
                    "enabled": True,
                    "allow_continue": allow_continue,
                    "allow_notify": True,
                }
            }
        },
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(
    db,
    *,
    loop_mode: str,
    user_text: str,
    assistant_text: str,
    provider: str = "claude",
):
    session_id = uuid4()
    started_at = _now()
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project="zerg",
        cwd="/tmp/zerg",
        started_at=started_at,
        ended_at=started_at,
        loop_mode=loop_mode,
    )
    db.add(session)
    db.flush()
    db.add(
        AgentEvent(
            session_id=session_id,
            role="user",
            content_text=user_text,
            timestamp=started_at,
        )
    )
    db.add(
        AgentEvent(
            session_id=session_id,
            role="assistant",
            content_text=assistant_text,
            timestamp=started_at,
        )
    )
    db.commit()
    return session_id


@pytest.mark.asyncio
async def test_turn_review_records_bounded_continue_from_loop_controller(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_continue.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="The next step is a bounded same-session continue.",
            rationale="The assistant left exactly one obvious follow-up.",
            recommended_action="continue_session",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=11,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="autopilot",
            user_text="finish the session detail page",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "continue"
        assert review.execution_state == "would_auto_continue"
        assert review.recommended_action == "continue_session"
        assert review.status == "recorded"
        assert review.run_id is None


@pytest.mark.asyncio
async def test_turn_review_keeps_manual_mode_observe_only_even_for_escalation(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_manual_escalate.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="escalate",
            summary="A real product decision is required.",
            rationale="The assistant is asking for a human choice, not a routine continue.",
            recommended_action="escalate",
            blocked_reasons=("Meaningful product decision required.",),
            model_id="gpt-test",
            raw_response='{"decision":"escalate"}',
            loop_thread_id=12,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="manual",
            user_text="should we make the risky production migration now?",
            assistant_text="This looks like a risky production migration and needs your decision before I proceed.",
        )

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "escalate"
        assert review.execution_state == "observe_only"
        assert review.status == "recorded"
        assert review.run_id is None


@pytest.mark.asyncio
async def test_turn_review_dedupes_same_completed_assistant_turn(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_dedupe.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Continue the same session.",
            rationale="Same bounded next step remains.",
            recommended_action="continue_session",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=13,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="run the last test pass",
            assistant_text="Ready for phase 2. Say continue and I will run the pending targeted tests.",
        )

        first = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        second = await maybe_record_session_turn_review(db=db, session_id=str(session_id))

        assert first is not None
        assert second is not None
        assert first.id == second.id
        count = db.query(SessionTurnReview).filter(SessionTurnReview.session_id == session_id).count()
        assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("presence_state", ["needs_user", "blocked"])
async def test_turn_review_still_records_when_latest_presence_is_pause_state(monkeypatch, tmp_path, presence_state):
    SessionLocal = _make_db(tmp_path, f"turn_review_pause_{presence_state}.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Continue after this completed turn.",
            rationale="The next step is still bounded.",
            recommended_action="continue_session",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=14,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=False)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="finish the verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )
        db.add(
            SessionPresence(
                session_id=str(session_id),
                state=presence_state,
                provider="claude",
                project="zerg",
                updated_at=_now(),
            )
        )
        db.commit()

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "continue"
        assert review.execution_state == "awaiting_user_approval"
        assert review.status == "recorded"


@pytest.mark.asyncio
async def test_turn_review_marks_controller_failures(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_controller_failure.db")

    async def _boom(**_kwargs):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _boom)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="continue if the next step is obvious",
            assistant_text="Only targeted verification remains.",
        )

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "ask_user"
        assert review.status == "failed"
        assert review.reason == "controller_error"
        assert "Loop controller evaluation failed." in (review.blocked_reasons or [])
