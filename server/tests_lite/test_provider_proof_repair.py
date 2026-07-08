from __future__ import annotations

from datetime import datetime
from datetime import timezone

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import TimelineCard
from zerg.services.provider_proof_repair import repair_provider_proof_session_environments


def _make_db(tmp_path):
    db_path = tmp_path / "provider_proof_repair.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    environment: str = "cinder",
    cwd: str | None = None,
    first_user_message_preview: str | None = None,
) -> AgentSession:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        provider="opencode",
        environment=environment,
        project="zerg",
        cwd=cwd,
        started_at=now,
        last_activity_at=now,
        first_user_message_preview=first_user_message_preview,
        transcript_revision=1,
        summary_revision=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    db.add(
        TimelineCard(
            session_id=session.id,
            provider=session.provider,
            environment=session.environment,
            project=session.project,
            cwd=session.cwd,
            started_at=session.started_at,
            last_activity_at=session.last_activity_at,
            first_user_message_preview=session.first_user_message_preview,
            transcript_revision=session.transcript_revision,
            parser_revision="test",
        )
    )
    db.commit()
    return session


def _add_user_event(db, session: AgentSession, text: str) -> None:
    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text=text,
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()


def test_provider_proof_repair_dry_run_reports_without_mutating(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            first_user_message_preview="LONGHOUSE_OPENCODE_NOREPLY_dry_run",
        )

        result = repair_provider_proof_session_environments(db, apply=False)

        assert result.scanned_sessions == 1
        assert result.repairable_sessions == 1
        assert result.updated_sessions == 0
        assert result.updated_timeline_cards == 0
        assert result.session_ids == [str(session.id)]
        assert db.get(AgentSession, session.id).environment == "cinder"
        assert db.get(TimelineCard, session.id).environment == "cinder"
    finally:
        db.close()


def test_provider_proof_repair_apply_updates_session_and_timeline_card(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            cwd="/Users/david/.longhouse/canaries/provider-live/opencode/proof/workspace",
        )

        result = repair_provider_proof_session_environments(db, apply=True)
        db.commit()

        assert result.scanned_sessions == 1
        assert result.repairable_sessions == 1
        assert result.updated_sessions == 1
        assert result.updated_timeline_cards == 1
        assert db.get(AgentSession, session.id).environment == "test"
        assert db.get(TimelineCard, session.id).environment == "test"
    finally:
        db.close()


def test_provider_proof_repair_default_skips_event_only_rows(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        marker = _seed_session(db)
        _add_user_event(db, marker, "LONGHOUSE_MY-TOOL_NOREPLY_event_only")

        result = repair_provider_proof_session_environments(db, apply=True)
        db.commit()

        assert result.scanned_sessions == 0
        assert result.repairable_sessions == 0
        assert result.updated_sessions == 0
        assert db.get(AgentSession, marker.id).environment == "cinder"
    finally:
        db.close()


def test_provider_proof_repair_event_scan_uses_event_text_and_skips_false_positives(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        marker = _seed_session(db)
        false_positive = _seed_session(db)
        _add_user_event(db, marker, "LONGHOUSE_MY-TOOL_NOREPLY_event_only")
        _add_user_event(db, false_positive, "LONGHOUSE__NOREPLY_not_a_provider_marker")

        result = repair_provider_proof_session_environments(db, apply=True, include_event_scan=True)
        db.commit()

        assert result.scanned_sessions == 2
        assert result.repairable_sessions == 1
        assert result.updated_sessions == 1
        assert result.skipped_false_positives == 1
        assert result.session_ids == [str(marker.id)]
        assert db.get(AgentSession, marker.id).environment == "test"
        assert db.get(AgentSession, false_positive.id).environment == "cinder"
    finally:
        db.close()


def test_provider_proof_repair_ignores_already_test_or_e2e_sessions(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        _seed_session(
            db,
            environment="test",
            first_user_message_preview="LONGHOUSE_OPENCODE_NOREPLY_already_test",
        )
        _seed_session(
            db,
            environment="e2e",
            first_user_message_preview="LONGHOUSE_OPENCODE_NOREPLY_already_e2e",
        )

        result = repair_provider_proof_session_environments(db, apply=True)

        assert result.scanned_sessions == 0
        assert result.repairable_sessions == 0
        assert result.updated_sessions == 0
        assert result.updated_timeline_cards == 0
    finally:
        db.close()
