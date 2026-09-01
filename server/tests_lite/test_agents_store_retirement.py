"""Guards for the deleted v1 store and its durable-head replacement query."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import initialize_database  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.models.agents import AgentEvent  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.agents import AgentSessionBranch  # noqa: E402
from zerg.services.managed_local_event_polling import latest_durable_head_event_id  # noqa: E402

_SERVER_ZERG = Path(__file__).resolve().parents[1] / "zerg"


def _files_naming_agents_store() -> set[str]:
    found: set[str] = set()
    for path in _SERVER_ZERG.rglob("*.py"):
        if "AgentsStore" in path.read_text(encoding="utf-8"):
            found.add(path.relative_to(_SERVER_ZERG).as_posix())
    return found


def test_agents_store_is_deleted_and_has_no_consumers():
    assert not (_SERVER_ZERG / "services" / "agents" / "store.py").exists()
    assert _files_naming_agents_store() == set()


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'agents_store_retirement.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_two_branch_session(db, *, now):
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="production",
        project="retirement",
        device_id="cinder",
        cwd="/tmp/retirement",
        started_at=now - timedelta(minutes=10),
    )
    db.add(session)
    db.flush()

    head = AgentSessionBranch(session_id=session.id, branch_reason="root", is_head=1)
    stale = AgentSessionBranch(session_id=session.id, branch_reason="rewind", is_head=0)
    db.add_all([head, stale])
    db.flush()

    def _event(*, branch_id, offset, event_origin):
        return AgentEvent(
            session_id=session.id,
            branch_id=branch_id,
            role="assistant",
            content_text=f"e{offset}",
            timestamp=now - timedelta(minutes=10) + timedelta(seconds=offset),
            event_origin=event_origin,
        )

    head_durable = _event(branch_id=head.id, offset=1, event_origin="durable")
    head_provisional = _event(branch_id=head.id, offset=2, event_origin="provisional")
    abandoned = _event(branch_id=stale.id, offset=3, event_origin="durable")
    db.add_all([head_durable, head_provisional, abandoned])
    db.commit()
    return session.id, int(head_durable.id), int(head_provisional.id), int(abandoned.id)


def test_latest_durable_head_event_id_ignores_provisional_and_abandoned_branches(tmp_path):
    session_factory = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with session_factory() as db:
        session_id, head_durable_id, head_provisional_id, abandoned_id = _seed_two_branch_session(db, now=now)

        latest = latest_durable_head_event_id(db, session_id)

        # The abandoned branch and the provisional row both carry *higher* ids,
        # so a query that dropped either filter would return one of them.
        assert abandoned_id > head_provisional_id > head_durable_id
        assert latest == head_durable_id


def test_latest_durable_head_event_id_is_zero_for_a_session_with_no_events(tmp_path):
    session_factory = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with session_factory() as db:
        session = AgentSession(
            id=uuid4(),
            provider="claude",
            environment="production",
            project="retirement-empty",
            device_id="cinder",
            cwd="/tmp/retirement",
            started_at=now,
        )
        db.add(session)
        db.commit()

        assert latest_durable_head_event_id(db, session.id) == 0
