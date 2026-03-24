from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from zerg.jobs import queue as job_queue
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.models import CommisJob
from zerg.models.user import User
from zerg.services.commis_job_queue import claim_jobs


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeCommisSession:
    def __init__(self, queued_ids: list[int]):
        self._queued_ids = queued_ids
        self.execute_calls: list[tuple[object, dict | None]] = []
        self.commit_calls = 0

    def query(self, *_args, **_kwargs):
        return _FakeQuery([SimpleNamespace(id=value) for value in self._queued_ids])

    def execute(self, stmt, params=None):
        self.execute_calls.append((stmt, params))
        return SimpleNamespace(fetchall=lambda: [(value,) for value in self._queued_ids])

    def commit(self):
        self.commit_calls += 1


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows[0]


class _FakeQueueConn:
    def __init__(self, *, candidate_id=None, claimed_row=None):
        self.candidate_id = candidate_id
        self.claimed_row = claimed_row
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))
        if sql.lstrip().startswith("SELECT id FROM job_queue"):
            row = {"id": self.candidate_id} if self.candidate_id is not None else None
            return _FakeCursor([] if row is None else [row])
        if sql.lstrip().startswith("UPDATE job_queue"):
            return _FakeCursor([] if self.claimed_row is None else [self.claimed_row])
        raise AssertionError(f"Unexpected SQL: {sql}")


def test_claim_jobs_returns_without_write_when_commis_queue_empty():
    db = _FakeCommisSession([])

    claimed = claim_jobs(db, 5, "worker-1")

    assert claimed == []
    assert db.execute_calls == []
    assert db.commit_calls == 0


def test_claim_jobs_updates_selected_commis_rows():
    db = _FakeCommisSession([11, 12])

    claimed = claim_jobs(db, 5, "worker-1")

    assert claimed == [11, 12]
    assert len(db.execute_calls) == 1
    _stmt, params = db.execute_calls[0]
    assert params == {"job_ids": [11, 12]}
    assert db.commit_calls == 1


def test_claim_jobs_updates_real_commis_rows(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/commis_claims.db")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    with SessionLocal() as db:
        user = User(id=1, email="owner@example.com", role="USER")
        db.add(user)
        db.commit()

        db.add_all(
            [
                CommisJob(owner_id=user.id, task="first", status="queued"),
                CommisJob(owner_id=user.id, task="second", status="queued"),
            ]
        )
        db.commit()

        claimed = claim_jobs(db, 5, "worker-1")
        claimed_rows = db.query(CommisJob).filter(CommisJob.id.in_(claimed)).order_by(CommisJob.id.asc()).all()

        assert len(claimed) == 2
        assert [row.status for row in claimed_rows] == ["running", "running"]
        assert all(row.worker_id == "worker-1" for row in claimed_rows)

    engine.dispose()


def test_claim_next_job_skips_update_when_job_queue_empty(monkeypatch):
    fake_conn = _FakeQueueConn()

    monkeypatch.setattr(job_queue, "_run_with_conn", lambda func, *args, **kwargs: func(fake_conn, *args, **kwargs))

    claimed = job_queue._claim_next_job_sync(job_queue.QueueOwner(name="tester", pid=1))

    assert claimed is None
    assert [sql for sql, _params in fake_conn.calls] == [
        "SELECT id FROM job_queue WHERE ( status = 'queued' AND scheduled_for <= :now ) "
        "OR ( status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= :now) ) "
        "ORDER BY scheduled_for ASC, created_at ASC LIMIT 1"
    ]


def test_claim_next_job_updates_selected_queue_row(monkeypatch):
    fake_conn = _FakeQueueConn(
        candidate_id="queue-1",
        claimed_row={
            "id": "queue-1",
            "job_id": "demo.job",
            "scheduled_for": "2026-03-24T00:00:00+00:00",
            "attempts": 1,
            "max_attempts": 3,
            "status": "running",
            "last_error": None,
        },
    )

    monkeypatch.setattr(job_queue, "_run_with_conn", lambda func, *args, **kwargs: func(fake_conn, *args, **kwargs))

    claimed = job_queue._claim_next_job_sync(job_queue.QueueOwner(name="tester", pid=1))

    assert claimed is not None
    assert claimed.id == "queue-1"
    assert claimed.job_id == "demo.job"
    assert [sql for sql, _params in fake_conn.calls] == [
        "SELECT id FROM job_queue WHERE ( status = 'queued' AND scheduled_for <= :now ) "
        "OR ( status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= :now) ) "
        "ORDER BY scheduled_for ASC, created_at ASC LIMIT 1",
        "UPDATE job_queue SET status = 'running', attempts = attempts + 1, lease_owner = :lease_owner, "
        "lease_expires_at = :lease_expires_at, started_at = COALESCE(started_at, :now), updated_at = :now "
        "WHERE id = :id AND ( status = 'queued' OR (status = 'running' AND (lease_expires_at IS NULL "
        "OR lease_expires_at <= :now)) ) RETURNING id, job_id, scheduled_for, attempts, max_attempts, "
        "status, last_error",
    ]
    assert fake_conn.calls[1][1]["id"] == "queue-1"


def test_claim_next_job_sync_claims_real_job_queue_row(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_QUEUE_DB_URL", f"sqlite:///{tmp_path}/job_queue_claims.db")

    scheduled_for = datetime.now(timezone.utc) - timedelta(seconds=1)
    queue_id = job_queue._enqueue_job_sync(
        "demo.job",
        scheduled_for,
        dedupe_key=job_queue.make_dedupe_key("demo.job", scheduled_for),
        max_attempts=3,
    )

    claimed = job_queue._claim_next_job_sync(job_queue.QueueOwner(name="tester", pid=1))

    assert claimed is not None
    assert claimed.id == queue_id
    assert claimed.job_id == "demo.job"
    assert claimed.status == "running"
