"""Unit tests for machine name filter in AgentsStore.

Verifies:
- get_distinct_filters() returns machines field
- Sessions tagged with machine names appear in machine list
- Filtering sessions by machine name (environment field) works
- machines list dedups correctly
"""

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _make_db(tmp_path):
    db_path = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _ingest(store, machine_name, project="test", provider="claude"):
    store.ingest_session(
        SessionIngest(
            provider=provider,
            environment=machine_name,
            project=project,
            device_id=f"device-{machine_name}",
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

        filters = store.get_distinct_filters(days_back=30)

        assert "machines" in filters
        assert "work-macbook" in filters["machines"]
        assert "home-server" in filters["machines"]


def test_machines_list_is_deduplicated(tmp_path):
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        # Ingest two sessions from the same machine
        _ingest(store, "work-macbook", project="proj-a")
        _ingest(store, "work-macbook", project="proj-b")

        filters = store.get_distinct_filters(days_back=30)

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


def test_filter_sessions_by_machine_name(tmp_path):
    """Sessions can be filtered by machine name via the environment field."""
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        _ingest(store, "work-macbook")
        _ingest(store, "home-server")

        # Filter by work-macbook
        sessions, total = store.list_sessions(environment="work-macbook", hide_autonomous=False)
        assert total == 1
        assert sessions[0].environment == "work-macbook"

        # Filter by home-server
        sessions, total = store.list_sessions(environment="home-server", hide_autonomous=False)
        assert total == 1
        assert sessions[0].environment == "home-server"

        # Unknown machine → no results
        sessions, total = store.list_sessions(environment="nonexistent", hide_autonomous=False)
        assert total == 0


def test_machines_excludes_null_environment(tmp_path):
    """Sessions with null environment are excluded from machine list."""
    Session = _make_db(tmp_path)
    with Session() as db:
        store = AgentsStore(db)
        # Ingest a session with a real machine name
        _ingest(store, "real-machine")

        filters = store.get_distinct_filters(days_back=30)

        # Only real machine name should appear, not None/empty
        assert None not in filters["machines"]
        assert "" not in filters["machines"]
        assert "real-machine" in filters["machines"]


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
