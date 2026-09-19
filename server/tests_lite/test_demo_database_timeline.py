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
