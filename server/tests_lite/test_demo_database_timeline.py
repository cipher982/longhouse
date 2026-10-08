"""The public demo corpus must be visible on the default timeline.

Demo sessions were once seeded with automation/test launch labels, which the
timeline hides as QA noise, so longhouse.ai's demo showed an empty timeline.
"""

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.store import CatalogStore
from zerg.services.demo_database import build_demo_database


def test_demo_database_sessions_show_on_default_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_STORAGE_V2_ROOT", str(tmp_path / "objects"))
    paths = build_demo_database(tmp_path / "longhouse-demo.db")

    engine = create_catalog_engine(paths["live"])
    try:
        store = CatalogStore(engine)
        page = store.list_session_timeline(
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=False,
            include_automation=False,
            device_id=None,
            days_back=30,
            limit=50,
            offset=0,
            owner_id=1,
            include_state_heads=True,
        )
        title_health = store.read_storage_title_dependency_health()
    finally:
        engine.dispose()

    assert page["total"] > 0
    assert title_health["status"] == "healthy"
    assert title_health["pending_sessions"] == 0


def test_demo_mode_serve_builds_corpus_only_where_no_database_exists(tmp_path, monkeypatch):
    from zerg.cli import serve

    built: list[str] = []
    monkeypatch.setattr(serve, "_build_demo_db", lambda path: built.append(str(path)))

    fresh = tmp_path / "fresh" / "longhouse.db"
    serve._ensure_demo_corpus(f"sqlite:///{fresh}")
    assert built == [str(fresh)]

    existing = tmp_path / "existing.db"
    existing.write_bytes(b"")
    serve._ensure_demo_corpus(f"sqlite:///{existing}")
    serve._ensure_demo_corpus("postgresql://example/db")
    assert built == [str(fresh)]


def test_demo_mode_requested_follows_demo_mode_env(monkeypatch):
    from zerg.cli import serve

    monkeypatch.delenv("APP_MODE", raising=False)
    monkeypatch.setenv("DEMO_MODE", "1")
    assert serve._demo_mode_requested() is True


def test_demo_corpus_lists_all_of_its_aged_sessions(monkeypatch, tmp_path):
    """longhouse.ai went empty when its corpus, built once, aged out of the recent window."""
    from datetime import UTC
    from datetime import datetime

    from zerg.services.session_listing_types import DEFAULT_LIST_DAYS_BACK
    from zerg.services.session_listing_types import resolve_search_days_back

    monkeypatch.delenv("APP_MODE", raising=False)
    monkeypatch.delenv("LONGHOUSE_DEMO_CORPUS", raising=False)
    monkeypatch.setenv("DEMO_MODE", "0")
    assert resolve_search_days_back(None, has_query=False) == DEFAULT_LIST_DAYS_BACK
    monkeypatch.setenv("LONGHOUSE_DEMO_CORPUS", "1")  # serve --demo
    assert resolve_search_days_back(None, has_query=False) == 3650
    monkeypatch.delenv("LONGHOUSE_DEMO_CORPUS")
    monkeypatch.setenv("DEMO_MODE", "1")  # the public demo
    assert resolve_search_days_back(None, has_query=False) == 3650
    assert resolve_search_days_back(7, has_query=False) == 7

    monkeypatch.setenv("LONGHOUSE_STORAGE_V2_ROOT", str(tmp_path / "objects"))
    paths = build_demo_database(tmp_path / "longhouse-demo.db", anchor=datetime(2026, 9, 22, tzinfo=UTC))
    engine = create_catalog_engine(paths["live"])
    try:
        page = CatalogStore(engine).list_session_timeline(
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=False,
            include_automation=False,
            device_id=None,
            days_back=resolve_search_days_back(None, has_query=False),
            limit=50,
            offset=0,
            owner_id=1,
            include_state_heads=True,
        )
    finally:
        engine.dispose()
    assert page["total"] > 0


def test_demo_corpus_is_built_straight_into_storage_v2_with_stable_ids(tmp_path, monkeypatch):
    """Every seeded demo event renders, under session ids that do not change between builds."""
    from datetime import UTC
    from datetime import datetime

    from sqlalchemy import select

    from zerg.catalogd.models import RenderObject
    from zerg.catalogd.models import StorageSession
    from zerg.services.demo_seed import DEMO_PRESENTATION
    from zerg.services.demo_sessions import build_demo_agent_sessions

    anchor = datetime(2026, 1, 15, 9, 41, tzinfo=UTC)
    built = []
    for name in ("first", "second"):
        monkeypatch.setenv("LONGHOUSE_STORAGE_V2_ROOT", str(tmp_path / name / "objects"))
        paths = build_demo_database(tmp_path / name / "longhouse-demo.db", anchor=anchor)
        engine = create_catalog_engine(paths["live"])
        try:
            with engine.connect() as connection:
                sessions = {str(row["session_id"]): row for row in connection.execute(select(StorageSession.__table__)).mappings()}
                events = {
                    session_id: sum(
                        int(count)
                        for (count,) in connection.execute(
                            select(RenderObject.__table__.c.event_count).where(
                                RenderObject.__table__.c.session_id == session_id,
                                RenderObject.__table__.c.generation_id == row["current_render_generation"],
                            )
                        )
                    )
                    for session_id, row in sessions.items()
                }
        finally:
            engine.dispose()
        built.append((sessions, events))

    (first, first_events), (second, _) = built
    assert set(first) == set(second)
    assert {row["render_state"] for row in first.values()} == {"ready"}
    assert {row["summary_title"] for row in first.values()} == {title for title, _ in DEMO_PRESENTATION.values()}
    assert sum(first_events.values()) == sum(len(session.events) for session in build_demo_agent_sessions(anchor))
