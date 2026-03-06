from datetime import datetime
from datetime import timezone

import pytest
from sqlalchemy import text

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import Run
from zerg.models import Thread
from zerg.models import User
from zerg.models.enums import ThreadType
from zerg.models.enums import UserRole
from zerg.models.models import Fiche
from zerg.services.fiche_state_recovery import perform_startup_run_recovery
from zerg.services.oikos_service import OikosService


def _make_db(tmp_path):
    db_path = tmp_path / "test_fiche_state_recovery.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_owner_graph(db):
    user = User(email="recovery@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    fiche = Fiche(
        name="Recovery Test",
        status="idle",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-scripted",
        owner_id=user.id,
    )
    db.add(fiche)
    db.commit()
    db.refresh(fiche)

    thread = Thread(
        fiche_id=fiche.id,
        title="Recovery Thread",
        thread_type=ThreadType.CHAT.value,
    )
    db.add(thread)
    db.commit()
    db.refresh(thread)

    return user, fiche, thread


@pytest.mark.asyncio
async def test_startup_run_recovery_handles_malformed_assistant_message_id(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, fiche, thread = _seed_owner_graph(db)
        started_at = datetime(2026, 3, 6, 4, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        result = db.execute(
            text(
                """
                INSERT INTO runs (
                    fiche_id, thread_id, status, trigger, started_at, assistant_message_id, model
                ) VALUES (
                    :fiche_id, :thread_id, :status, :trigger, :started_at, :assistant_message_id, :model
                )
                """
            ),
            {
                "fiche_id": fiche.id,
                "thread_id": thread.id,
                "status": "RUNNING",
                "trigger": "API",
                "started_at": started_at,
                "assistant_message_id": "live-voice-1772742439",
                "model": "gpt-scripted",
            },
        )
        db.commit()
        run_id = result.lastrowid

    monkeypatch.setattr("zerg.services.fiche_state_recovery.get_session_factory", lambda: SessionLocal)

    recovered_ids = await perform_startup_run_recovery()

    assert recovered_ids == [run_id]

    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT status, error, finished_at, duration_ms, assistant_message_id FROM runs WHERE id = :run_id"
            ),
            {"run_id": run_id},
        ).mappings().one()

    assert row["status"] == "FAILED"
    assert row["error"] == "Orphaned after server restart - execution state lost"
    assert row["finished_at"] is not None
    assert row["duration_ms"] is not None
    assert row["assistant_message_id"] == "live-voice-1772742439"


@pytest.mark.asyncio
async def test_run_oikos_rejects_non_uuid_message_id_before_creating_rows(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = User(email="uuid@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        service = OikosService(db)

        with pytest.raises(ValueError, match="message_id must be a UUID"):
            await service.run_oikos(
                owner_id=user.id,
                task="hello",
                message_id="live-web-1772742444",
            )

        assert db.query(Run).count() == 0
