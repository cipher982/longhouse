from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurnReview
from zerg.models.enums import UserRole
from zerg.models.loop_push_subscription import LoopPushSubscription
from zerg.models.user import User
from zerg.services.loop_push import send_loop_push_nudge
from zerg.services.loop_push import upsert_loop_push_subscription


def _make_db(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_review(db):
    user = User(email=f"loop-push-{uuid4()}@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    started_at = datetime.now(timezone.utc)
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="zerg",
        device_id="cinder",
        cwd="/tmp/zerg",
        started_at=started_at,
        ended_at=started_at,
        summary_title="Hiring",
        loop_mode="assist",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    review = SessionTurnReview(
        session_id=session.id,
        owner_id=user.id,
        assistant_event_id=2,
        turn_index=1,
        trigger_type="turn.completed",
        loop_mode="assist",
        decision="continue",
        summary="Only targeted verification remains.",
        rationale="Routine continue case.",
        execution_state="awaiting_user_approval",
        recommended_action="continue_session",
        follow_up_prompt="Run the pending targeted tests.",
        status="enqueued",
        reason="notify_user",
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return user, session, review


def test_send_loop_push_nudge_marks_success(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "loop_push_success.db")
    sent_payloads: list[dict] = []

    monkeypatch.setattr(
        "zerg.services.loop_push.get_settings",
        lambda: SimpleNamespace(
            loop_push_enabled=True,
            loop_push_vapid_private_key="PRIVATE",
            loop_push_vapid_subject="mailto:test@example.com",
        ),
    )

    def _fake_webpush(**kwargs):
        sent_payloads.append(kwargs)
        return None

    monkeypatch.setattr("zerg.services.loop_push.webpush", _fake_webpush)

    with SessionLocal() as db:
        user, session, review = _seed_review(db)
        upsert_loop_push_subscription(
            db=db,
            owner_id=user.id,
            subscription={
                "endpoint": "https://push.example/sub/1",
                "keys": {"p256dh": "p256dh", "auth": "auth"},
            },
            install_id="install-1",
            user_agent="Loop Test",
        )

        result = send_loop_push_nudge(db=db, owner_id=user.id, review=review, session=session)
        assert result is True
        assert len(sent_payloads) == 1

        row = db.query(LoopPushSubscription).one()
        assert row.last_push_at is not None
        assert row.last_error is None
        assert row.revoked_at is None


def test_send_loop_push_nudge_revokes_gone_subscription(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "loop_push_gone.db")

    monkeypatch.setattr(
        "zerg.services.loop_push.get_settings",
        lambda: SimpleNamespace(
            loop_push_enabled=True,
            loop_push_vapid_private_key="PRIVATE",
            loop_push_vapid_subject="mailto:test@example.com",
        ),
    )

    class _FakeResponse:
        status_code = 410
        text = "gone"

    from pywebpush import WebPushException

    def _fake_webpush(**_kwargs):
        raise WebPushException("gone", response=_FakeResponse())

    monkeypatch.setattr("zerg.services.loop_push.webpush", _fake_webpush)

    with SessionLocal() as db:
        user, session, review = _seed_review(db)
        upsert_loop_push_subscription(
            db=db,
            owner_id=user.id,
            subscription={
                "endpoint": "https://push.example/sub/1",
                "keys": {"p256dh": "p256dh", "auth": "auth"},
            },
            install_id="install-1",
            user_agent="Loop Test",
        )

        result = send_loop_push_nudge(db=db, owner_id=user.id, review=review, session=session)
        assert result is False

        row = db.query(LoopPushSubscription).one()
        assert row.revoked_at is not None
        assert row.last_error is not None
