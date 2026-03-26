from __future__ import annotations

from types import SimpleNamespace

import pytest

from zerg.database import Base
from zerg.database import get_test_commis_id
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.models import CommisJob
from zerg.models.user import User
from zerg.services.commis_job_processor import CommisJobProcessor
from zerg.services.write_serializer import WriteSerializer


def _make_db(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


@pytest.mark.asyncio
async def test_process_pending_jobs_claims_e2e_commis_db(monkeypatch, tmp_path):
    base_factory = _make_db(tmp_path, "commis_processor_base.db")
    commis_factory = _make_db(tmp_path, "commis_processor_commis.db")

    with commis_factory() as db:
        owner = User(email="processor@example.com")
        db.add(owner)
        db.commit()
        db.refresh(owner)

        job = CommisJob(owner_id=owner.id, task="Check disk usage", status="queued")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    serializer = WriteSerializer()
    serializer.configure_resolver(lambda: commis_factory if get_test_commis_id() == "0" else base_factory)

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: serializer)
    monkeypatch.setattr(
        "zerg.services.commis_job_processor.get_settings",
        lambda: SimpleNamespace(testing=True, environment="test:e2e"),
    )
    monkeypatch.setattr("zerg.services.commis_job_processor.list_test_commis_ids", lambda: ["0"])

    processor = CommisJobProcessor()
    processed: list[tuple[str | None, int]] = []

    async def _fake_heartbeat(commis_id: str | None, pending_job_id: int) -> None:
        return None

    async def _fake_process(commis_id: str | None, pending_job_id: int) -> None:
        processed.append((commis_id, pending_job_id))

    monkeypatch.setattr(processor, "_heartbeat_loop", _fake_heartbeat)
    monkeypatch.setattr(processor, "_process_job_with_cleanup", _fake_process)

    await processor._process_pending_jobs()

    assert processed == [("0", job_id)]

    with commis_factory() as db:
        stored = db.query(CommisJob).filter(CommisJob.id == job_id).one()
        assert stored.status == "running"
        assert stored.worker_id == processor._worker_id

    with base_factory() as db:
        assert db.query(CommisJob).count() == 0
