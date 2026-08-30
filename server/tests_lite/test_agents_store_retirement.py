"""Guards for the retirement of ``AgentsStore``, the legacy in-process data path.

Two separate claims live here.

1. No Runtime Host route reaches ``AgentsStore`` any more. What is left is a
   frozen allowlist of modules that only the QA harness and the demo image
   build can reach; adding a new consumer, or reaching one from a router, has
   to fail here rather than quietly re-growing the legacy path.
2. ``latest_durable_head_event_id`` -- the query that replaced
   ``AgentsStore.get_latest_event_id`` on the managed-local control path --
   still scopes to the head branch and still ignores provisional events.
"""

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

# Every file under server/zerg/ still allowed to name ``AgentsStore``.
#
# ``services/agents/`` is the store itself. The other four service modules hold
# functions that are now reachable only from ``qa/universal_agent_harness.py``:
# its ``db_ingest_project`` scenario still proves ingest through
# ``AgentsStore.ingest_session``, a write path production answers 426 for.
# ``services/demo_seed.py`` writes the legacy corpus at image build time.
# Both are pending owner decisions; when either lands, delete its entry here
# rather than widening the set.
_ALLOWED_AGENTS_STORE_FILES = frozenset(
    {
        "qa/universal_agent_harness.py",
        "services/agents/__init__.py",
        "services/agents/store.py",
        "services/demo_seed.py",
        "services/session_listing.py",
        "services/session_response_projection.py",
        "services/session_views.py",
        "services/timeline_session_listing.py",
    }
)


def _files_naming_agents_store() -> set[str]:
    found: set[str] = set()
    for path in _SERVER_ZERG.rglob("*.py"):
        if "AgentsStore" in path.read_text(encoding="utf-8"):
            found.add(path.relative_to(_SERVER_ZERG).as_posix())
    return found


def test_agents_store_consumers_are_a_frozen_allowlist():
    actual = _files_naming_agents_store()

    new_consumers = sorted(actual - _ALLOWED_AGENTS_STORE_FILES)
    assert new_consumers == [], f"new AgentsStore consumers must not be added: {new_consumers}"

    retired = sorted(_ALLOWED_AGENTS_STORE_FILES - actual)
    assert retired == [], f"these files no longer use AgentsStore; drop them from the allowlist: {retired}"


def test_no_router_names_agents_store():
    routers = {name for name in _files_naming_agents_store() if name.startswith("routers/")}
    assert routers == set(), f"/api routes must not reach the legacy store: {sorted(routers)}"


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
