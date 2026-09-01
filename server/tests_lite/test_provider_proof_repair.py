from __future__ import annotations

from datetime import datetime
from datetime import timezone

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
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
    device_id: str | None = None,
    first_user_message_preview: str | None = None,
) -> AgentSession:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        provider="opencode",
        environment=environment,
        project="zerg",
        cwd=cwd,
        device_id=device_id,
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


def test_provider_proof_repair_dry_run_reports_without_mutating(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        # The provider-factory machine id is a supported classification signal;
        # prompt text is not.
        session = _seed_session(db, device_id="provider-factory-resume")

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


def test_provider_proof_repair_catches_build_canary_and_reviewed_proof_worktree(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        build_canary = _seed_session(
            db,
            cwd="/Users/david/git/zerg/longhouse/.build/canaries/provider-live/claude/20260701T210350Z/claude-live-token-contract/workspace",
        )
        proof_worktree = _seed_session(
            db,
            cwd="/Users/david/git/_wt/longhouse-provider-live-proof-owner",
        )
        visible = _seed_session(
            db,
            cwd="/Users/david/git/zerg/longhouse",
            first_user_message_preview="Please debug provider live proof without hiding this real session.",
        )

        result = repair_provider_proof_session_environments(db, apply=True)
        db.commit()

        assert result.scanned_sessions == 2
        assert result.repairable_sessions == 2
        assert result.updated_sessions == 2
        assert db.get(AgentSession, build_canary.id).environment == "test"
        assert db.get(AgentSession, proof_worktree.id).environment == "test"
        assert db.get(AgentSession, visible.id).environment == "cinder"
    finally:
        db.close()


def test_provider_proof_repair_never_reclassifies_from_prompt_text(tmp_path):
    """Only cwd/machine namespace repairs a session; prompt text never does.

    Repair has to agree with the classifier: a user session that happens to
    contain proof-looking prompt text stays the user's session.
    """

    factory = _make_db(tmp_path)
    db = factory()
    try:
        ordinary = _seed_session(
            db,
            cwd="/Users/david/git/zerg/longhouse",
            first_user_message_preview="LONGHOUSE_OPENCODE_NOREPLY_looks_like_a_marker",
        )

        result = repair_provider_proof_session_environments(db, apply=True)
        db.commit()

        # scanned_sessions == 0 is the load-bearing one: it proves the marker
        # never made this row a candidate, rather than that it was a candidate
        # the classifier later dropped.
        assert result.scanned_sessions == 0
        assert result.repairable_sessions == 0
        assert result.updated_sessions == 0
        assert result.updated_timeline_cards == 0
        assert result.session_ids == []
        assert db.get(AgentSession, ordinary.id).environment == "cinder"
        assert db.get(TimelineCard, ordinary.id).environment == "cinder"
    finally:
        db.close()


def test_provider_proof_repair_ignores_already_test_or_e2e_sessions(tmp_path):
    factory = _make_db(tmp_path)
    db = factory()
    try:
        # Both carry a real proof cwd, so only the environment filter can
        # exclude them.
        _seed_session(
            db,
            environment="test",
            cwd="/Users/david/.longhouse/canaries/provider-live/opencode/proof/workspace",
        )
        _seed_session(
            db,
            environment="e2e",
            cwd="/Users/david/git/_wt/longhouse-provider-live-proof-owner",
        )

        result = repair_provider_proof_session_environments(db, apply=True)

        assert result.scanned_sessions == 0
        assert result.repairable_sessions == 0
        assert result.updated_sessions == 0
        assert result.updated_timeline_cards == 0
    finally:
        db.close()
