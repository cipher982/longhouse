"""Unit tests for machine filter in AgentsStore.

Verifies:
- get_distinct_filters() returns a machines field sourced from enrolled device_ids
- machines list dedups and sorts correctly
- Filtering sessions by device_id is strict (no environment fallback)
"""

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.database import Base
from zerg.models import User
from zerg.models.device_token import DeviceToken
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _make_db(tmp_path):
    db_path = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _enroll(db, device_id):
    """Ensure an owner + device token exists so the machine filter surfaces device_id."""
    if db.get(User, 1) is None:
        db.add(User(id=1, email="owner@example.com", role="ADMIN"))
        db.flush()
    db.add(DeviceToken(owner_id=1, device_id=device_id, token_hash=f"hash-{device_id}"))
    db.commit()


def _ingest(store, device_id, *, environment="production", project="test", provider="claude", enroll=True):
    if enroll:
        _enroll(store.db, device_id)
    store.ingest_session(
        SessionIngest(
            provider=provider,
            environment=environment,
            project=project,
            device_id=device_id,
            cwd="/tmp",
            git_repo=None,
            git_branch=None,
            started_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            events=[
                EventIngest(
                    role="user",
                    content_text="hello",
                    timestamp=datetime(2026, 3, 1, tzinfo=timezone.utc),
                    source_path="/tmp/session.jsonl",
                    source_offset=0,
                )
            ],
        )
    )


def test_get_distinct_filters_includes_machines(tmp_path):
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        _ingest(store, "work-macbook")
        _ingest(store, "home-server")

        filters = store.get_distinct_filters(days_back=9999)

        assert "machines" in filters
        assert "work-macbook" in filters["machines"]
        assert "home-server" in filters["machines"]


def test_machines_list_is_deduplicated(tmp_path):
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        # Ingest two sessions from the same machine (enroll once)
        _ingest(store, "work-macbook", project="proj-a")
        _ingest(store, "work-macbook", project="proj-b", enroll=False)

        filters = store.get_distinct_filters(days_back=9999)

        assert filters["machines"].count("work-macbook") == 1


def test_machines_list_is_sorted(tmp_path):
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        _ingest(store, "zz-server")
        _ingest(store, "aa-laptop")
        _ingest(store, "mm-desktop")

        filters = store.get_distinct_filters(days_back=30)

        assert filters["machines"] == sorted(filters["machines"])


def test_device_id_filter_is_strict(tmp_path):
    """list_sessions filters strictly on device_id, never on environment."""
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        _ingest(store, "cinder", environment="cinder")
        # Ghost row: dead device_id, environment matches the live machine name.
        _ingest(store, "shipper-laptop", environment="cinder", enroll=False)

        sessions, total = store.list_sessions(device_id="cinder", hide_autonomous=False)
        assert total == 1
        assert sessions[0].device_id == "cinder"

        # A value that only exists in `environment` matches nothing.
        sessions, total = store.list_sessions(device_id="shipper-laptop", hide_autonomous=False)
        device_ids = {s.device_id for s in sessions}
        assert device_ids == {"shipper-laptop"}  # matched its own device_id, not via environment


def test_machines_only_enrolled_devices(tmp_path):
    """Machine filter surfaces enrolled device_ids only; ghosts are excluded."""
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        _ingest(store, "real-machine")
        # Ghost device_id (not enrolled) must not appear as a filter chip.
        _ingest(store, "ghost-machine", environment="real-machine", enroll=False)

        filters = store.get_distinct_filters(days_back=9999)

        assert None not in filters["machines"]
        assert "" not in filters["machines"]
        assert "real-machine" in filters["machines"]
        assert "ghost-machine" not in filters["machines"]


def test_get_distinct_filters_returns_all_three_fields(tmp_path):
    """Smoke test: all three fields present even on empty DB."""
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        filters = store.get_distinct_filters(days_back=30)

        assert "projects" in filters
        assert "providers" in filters
        assert "machines" in filters
        assert isinstance(filters["machines"], list)
