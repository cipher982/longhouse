from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.notification_client_presence import NotificationClientPresence
from zerg.models.user import User
from zerg.services.notification_policy import AttentionDeliveryAction
from zerg.services.notification_policy import evaluate_tier1_delivery
from zerg.services.notification_policy import evaluate_tier2_delivery
from zerg.services.notification_policy import in_quiet_hours


def _make_db(tmp_path: Path):
    db_path = tmp_path / "notification-policy.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)()


def test_tier2_suppressed_by_visible_web_presence(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
        user = User(email="owner@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        session = AgentSession(
            provider="codex",
            environment="test",
            started_at=now,
        )
        db.add(session)
        db.flush()
        db.add(
            NotificationClientPresence(
                owner_id=user.id,
                client_id="web-1",
                client_type="web",
                visible=True,
                route="/timeline",
                session_id=str(session.id),
                last_seen_at=now,
            )
        )
        db.commit()

        decision = evaluate_tier2_delivery(db, user=user, session=session, occurred_at=now)
        assert decision.action == AttentionDeliveryAction.SUPPRESS
        assert decision.reason == "web_presence"
    finally:
        db.close()


def test_tier1_queues_during_quiet_hours_without_time_sensitive(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        now = datetime(2026, 7, 8, 23, 30, tzinfo=timezone.utc)
        user = User(
            email="owner@test.local",
            role="ADMIN",
            prefs={"quiet_hours_start": "22:00", "quiet_hours_end": "07:00", "timezone": "UTC"},
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        session = AgentSession(
            provider="codex",
            environment="test",
            started_at=now - timedelta(hours=1),
        )
        db.add(session)
        db.commit()

        assert in_quiet_hours(user, now) is True
        decision = evaluate_tier1_delivery(
            db,
            user=user,
            session=session,
            occurred_at=now,
            event_type="session_blocked",
        )
        assert decision.action == AttentionDeliveryAction.QUEUE
        assert decision.reason == "quiet_hours"
        assert decision.queue_until is not None
    finally:
        db.close()
