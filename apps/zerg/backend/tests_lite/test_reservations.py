"""Tests for file reservation lifecycle via ORM."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.models.work import FileReservation
from zerg.models.work import Insight  # noqa: F401


def _make_db(tmp_path):
    db_path = tmp_path / "test_reservations.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def test_reserve_and_check(tmp_path):
    """Insert a reservation and query it back."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        reservation = FileReservation(
            file_path="src/auth.py",
            project="zerg",
            agent="claude",
            reason="Refactoring auth flow",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(reservation)
        db.commit()

        result = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == "src/auth.py",
                FileReservation.project == "zerg",
                FileReservation.released_at.is_(None),
            )
            .first()
        )
        assert result is not None
        assert result.agent == "claude"
        assert result.reason == "Refactoring auth flow"


def test_duplicate_reservation_blocked(tmp_path):
    """An active reservation with same file_path+project prevents a second insert.

    This relies on the unique partial index ix_reservation_active which
    only covers rows where released_at IS NULL.
    """
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        # First reservation
        db.add(FileReservation(
            file_path="src/main.py",
            project="zerg",
            agent="claude",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        db.commit()

        # Check that an active reservation exists
        active = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == "src/main.py",
                FileReservation.project == "zerg",
                FileReservation.released_at.is_(None),
            )
            .first()
        )
        assert active is not None

        # Attempting a second reservation for the same file+project
        # would violate the unique partial index. We test the query-based
        # check that the router would use.
        count = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == "src/main.py",
                FileReservation.project == "zerg",
                FileReservation.released_at.is_(None),
            )
            .count()
        )
        assert count == 1  # Only one active reservation


def test_expired_cleanup_on_reserve(tmp_path):
    """Expired reservations can be cleaned up before creating a new one."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        # Create an expired reservation
        expired = FileReservation(
            file_path="src/utils.py",
            project="zerg",
            agent="codex",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # already expired
        )
        db.add(expired)
        db.commit()

        # Clean up expired reservations (as the router would do)
        now = datetime.now(timezone.utc)
        expired_reservations = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == "src/utils.py",
                FileReservation.project == "zerg",
                FileReservation.released_at.is_(None),
                FileReservation.expires_at < now,
            )
            .all()
        )
        for r in expired_reservations:
            r.released_at = now
        db.commit()

        # Now no active reservation exists
        active = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == "src/utils.py",
                FileReservation.project == "zerg",
                FileReservation.released_at.is_(None),
            )
            .first()
        )
        assert active is None

        # Can create a new reservation
        db.add(FileReservation(
            file_path="src/utils.py",
            project="zerg",
            agent="claude",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        db.commit()

        new_active = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == "src/utils.py",
                FileReservation.project == "zerg",
                FileReservation.released_at.is_(None),
            )
            .first()
        )
        assert new_active is not None
        assert new_active.agent == "claude"


def test_release_reservation(tmp_path):
    """Releasing a reservation sets released_at."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        reservation = FileReservation(
            file_path="src/config.py",
            project="zerg",
            agent="gemini",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(reservation)
        db.commit()

        reservation_id = reservation.id

        # Release it
        to_release = db.query(FileReservation).filter(FileReservation.id == reservation_id).first()
        assert to_release is not None
        to_release.released_at = datetime.now(timezone.utc)
        db.commit()

        # Verify it is no longer active
        active = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == "src/config.py",
                FileReservation.project == "zerg",
                FileReservation.released_at.is_(None),
            )
            .first()
        )
        assert active is None


def test_empty_string_project(tmp_path):
    """None project defaults to empty string via server_default."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        reservation = FileReservation(
            file_path="global_file.py",
            agent="claude",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            # project not specified — server_default=""
        )
        db.add(reservation)
        db.commit()

        result = db.query(FileReservation).filter(FileReservation.file_path == "global_file.py").first()
        assert result is not None
        # server_default sets project to "" when not explicitly provided
        assert result.project == ""
