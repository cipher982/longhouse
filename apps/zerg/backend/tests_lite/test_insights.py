"""Tests for insight CRUD via ORM (not HTTP)."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.models.work import FileReservation  # noqa: F401
from zerg.models.work import Insight


def _make_db(tmp_path):
    db_path = tmp_path / "test_insights.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def test_create_insight(tmp_path):
    """Basic insert and query of an insight."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        insight = Insight(
            insight_type="learning",
            title="SQLite WAL mode is important",
            description="Without WAL, concurrent writes block readers.",
            project="zerg",
            severity="info",
            confidence=0.9,
            tags=["sqlite", "performance"],
        )
        db.add(insight)
        db.commit()

        result = db.query(Insight).filter(Insight.project == "zerg").first()
        assert result is not None
        assert result.title == "SQLite WAL mode is important"
        assert result.insight_type == "learning"
        assert result.confidence == 0.9
        assert result.tags == ["sqlite", "performance"]


def test_dedup_within_7_days(tmp_path):
    """Same title+project within 7 days should find the existing insight for dedup."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        # Insert first insight
        insight1 = Insight(
            insight_type="pattern",
            title="Auth tokens expire after 1 hour",
            project="zerg",
            severity="info",
        )
        db.add(insight1)
        db.commit()

        # Query for existing insight with same title+project within 7 days
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        existing = (
            db.query(Insight)
            .filter(
                Insight.title == "Auth tokens expire after 1 hour",
                Insight.project == "zerg",
                Insight.created_at >= cutoff,
            )
            .first()
        )

        # Should find the existing one (dedup candidate)
        assert existing is not None
        assert str(existing.id) == str(insight1.id)


def test_no_dedup_after_7_days(tmp_path):
    """Old insight (>7 days) should not be found for dedup."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        # Insert insight with old created_at
        old_time = datetime.now(timezone.utc) - timedelta(days=8)
        insight_old = Insight(
            insight_type="pattern",
            title="Old pattern",
            project="zerg",
            severity="info",
        )
        db.add(insight_old)
        db.flush()

        # Manually set created_at to 8 days ago (bypassing server_default)
        from sqlalchemy import text as sa_text

        db.execute(
            sa_text("UPDATE insights SET created_at = :ts WHERE id = :id"),
            {"ts": old_time.isoformat(), "id": str(insight_old.id)},
        )
        db.commit()

        # Query for dedup candidate within 7 days
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        existing = (
            db.query(Insight)
            .filter(
                Insight.title == "Old pattern",
                Insight.project == "zerg",
                Insight.created_at >= cutoff,
            )
            .first()
        )

        # Should NOT find the old one
        assert existing is None


def test_query_by_project(tmp_path):
    """Filter insights by project."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        db.add(Insight(
            insight_type="learning",
            title="Zerg learning",
            project="zerg",
        ))
        db.add(Insight(
            insight_type="learning",
            title="HDR learning",
            project="hdr",
        ))
        db.commit()

        zerg_insights = db.query(Insight).filter(Insight.project == "zerg").all()
        assert len(zerg_insights) == 1
        assert zerg_insights[0].title == "Zerg learning"

        hdr_insights = db.query(Insight).filter(Insight.project == "hdr").all()
        assert len(hdr_insights) == 1
        assert hdr_insights[0].title == "HDR learning"


def test_query_by_type(tmp_path):
    """Filter insights by insight_type."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        db.add(Insight(
            insight_type="failure",
            title="Deploy failed on Coolify",
            project="zerg",
        ))
        db.add(Insight(
            insight_type="pattern",
            title="Always add UFW rule for Docker",
            project="zerg",
        ))
        db.add(Insight(
            insight_type="failure",
            title="Another failure",
            project="zerg",
        ))
        db.commit()

        failures = db.query(Insight).filter(Insight.insight_type == "failure").all()
        assert len(failures) == 2

        patterns = db.query(Insight).filter(Insight.insight_type == "pattern").all()
        assert len(patterns) == 1
        assert patterns[0].title == "Always add UFW rule for Docker"
