from __future__ import annotations

import pytest

import zerg.database as database_module
from zerg.database import get_catalog_session_factory
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.user import User
from zerg.services.write_serializer import get_catalog_write_serializer
from zerg.services.write_serializer import get_live_write_serializer


def test_catalog_factory_uses_live_database_without_opening_archive(tmp_path, monkeypatch):
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(live_engine)
    LiveSession = make_sessionmaker(live_engine)
    with LiveSession() as live_db:
        live_db.add(User(id=23, email="live-only@example.com", role="USER"))
        live_db.commit()

    monkeypatch.setattr(database_module._settings, "live_catalog_enabled", True)
    monkeypatch.setattr(database_module._settings, "live_database_url", str(live_engine.url))
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: LiveSession)

    def fail_archive_factory():
        raise AssertionError("cold archive factory must not be opened")

    monkeypatch.setattr(database_module, "get_session_factory", fail_archive_factory)

    factory = get_catalog_session_factory()
    with factory() as catalog_db:
        assert catalog_db.query(User).one().email == "live-only@example.com"


def test_catalog_factory_stays_archive_backed_until_explicit_cutover(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(database_module._settings, "live_catalog_enabled", False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: sentinel)
    monkeypatch.setattr(
        database_module,
        "get_live_session_factory",
        lambda: pytest.fail("dark live catalog must not be selected"),
    )
    assert get_catalog_session_factory() is sentinel


def test_catalog_serializer_follows_catalog_owner(monkeypatch):
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    assert get_catalog_write_serializer() is get_live_write_serializer()
