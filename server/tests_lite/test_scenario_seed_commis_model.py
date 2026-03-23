"""Regression tests for scenario seeding defaults."""

from unittest.mock import patch

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import CommisJob
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.scenarios.seed import seed_scenario


def _make_db(tmp_path):
    """Create a SQLite DB for scenario seed tests."""
    db_path = tmp_path / "test_scenario_seed.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def test_seed_scenario_commis_model_is_null_without_override(tmp_path):
    """Commis model should remain NULL unless the scenario explicitly sets one."""
    SessionLocal = _make_db(tmp_path)

    scenario = {
        "runs": [
            {
                "id": "run-1",
                "title": "Seeded run",
                "commis_jobs": [
                    {
                        "task": "Check inbox and summarize",
                        "status": "queued",
                    }
                ],
            }
        ]
    }

    with SessionLocal() as db:
        user = User(email="scenario-owner@local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        with patch("zerg.scenarios.seed.load_scenario", return_value=scenario):
            result = seed_scenario(
                db,
                "unit-scenario",
                owner_id=user.id,
                target="test",
                namespace="tests",
            )

        assert result["commis_jobs"] == 1
        job = db.query(CommisJob).one()
        assert job.model is None
