from datetime import datetime
from datetime import timezone
from uuid import uuid4

from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.session_preferences import load_session_preferences


def test_live_catalog_is_authoritative_for_session_preferences(monkeypatch, tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    LiveSession = make_sessionmaker(engine)
    session_id = uuid4()
    with LiveSession() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                started_at=datetime.now(timezone.utc),
                user_state="archived",
                loop_mode="autopilot",
                notification_muted=True,
            )
        )
        db.commit()

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    stale_archive = type(
        "ArchiveSession",
        (),
        {"user_state": "active", "loop_mode": "assist", "notification_muted": False},
    )()

    preferences = load_session_preferences(session_id, standalone_session=stale_archive)

    assert preferences.user_state == "archived"
    assert preferences.loop_mode == "autopilot"
    assert preferences.notification_muted is True


def test_missing_live_row_uses_canonical_defaults_not_archive(monkeypatch, tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    LiveSession = make_sessionmaker(engine)
    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    stale_archive = type(
        "ArchiveSession",
        (),
        {"user_state": "archived", "loop_mode": "autopilot", "notification_muted": True},
    )()

    preferences = load_session_preferences(uuid4(), standalone_session=stale_archive)

    assert preferences.user_state == "active"
    assert preferences.loop_mode == "assist"
    assert preferences.notification_muted is False


def test_catalog_mode_reads_preferences_without_opening_sqlite(monkeypatch):
    session_id = uuid4()
    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        "zerg.services.catalog_read_gateway.session_snapshot",
        lambda value: {
            "found": True,
            "facts": {
                "catalog": {
                    "session_id": value,
                "user_state": "snoozed",
                "loop_mode": "autopilot",
                "notification_muted": True,
                "user_hidden_from_timeline": True,
                }
            },
        },
    )
    monkeypatch.setattr(
        "zerg.database.get_live_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("API process must not open live SQLite")),
    )

    preferences = load_session_preferences(session_id)

    assert preferences.user_state == "snoozed"
    assert preferences.loop_mode == "autopilot"
    assert preferences.notification_muted is True
    assert preferences.user_hidden_from_timeline is True
