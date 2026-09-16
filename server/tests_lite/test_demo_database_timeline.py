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
        page = CatalogStore(engine).list_session_timeline(
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
    finally:
        engine.dispose()

    assert page["total"] > 0
