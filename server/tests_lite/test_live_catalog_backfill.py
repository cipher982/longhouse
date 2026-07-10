from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.models.agents import TimelineCard
from zerg.models.apns_device_registration import APNSDeviceRegistration
from zerg.models.apns_live_activity_registration import APNSLiveActivityRegistration
from zerg.models.apns_widget_push_state import APNSWidgetPushState
from zerg.models.device_token import DeviceToken
from zerg.models.enums import UserRole
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
from zerg.models.refresh_session import RefreshSession
from zerg.models.user import User
from zerg.services.live_catalog_backfill import backfill_live_catalog
from zerg.services.live_catalog_backfill import live_catalog_table_names
from zerg.services.live_catalog_backfill import sync_live_catalog_session


def test_live_catalog_backfill_is_idempotent_and_updates_rows(tmp_path):
    archive_engine = make_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(archive_engine)
    initialize_live_database(live_engine)
    ArchiveSession = make_sessionmaker(archive_engine)
    LiveSession = make_sessionmaker(live_engine)
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    device_token_id = uuid4()

    with ArchiveSession() as archive_db:
        user = User(
            id=7,
            email="catalog@example.com",
            role=UserRole.USER,
            prefs={"theme": "dark"},
            context={"machine": "laptop"},
        )
        archive_db.add(user)
        archive_db.flush()
        archive_db.add(
            APNSDeviceRegistration(
                id=uuid4(),
                owner_id=7,
                platform="ios",
                device_token="a" * 64,
                push_environment="sandbox",
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
        )
        archive_db.add(
            APNSLiveActivityRegistration(
                id=uuid4(),
                owner_id=7,
                session_id=str(session_id),
                activity_id="activity-1",
                push_token="b" * 64,
                push_environment="sandbox",
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
        )
        archive_db.add(
            APNSWidgetPushState(
                owner_id=7,
                state_hash="widget-state",
                created_at=now,
                updated_at=now,
            )
        )
        archive_db.add(
            RefreshSession(
                id=11,
                token_hash="r" * 64,
                user_id=7,
                family_id=str(uuid4()),
                created_at=now,
                absolute_expires_at=now + timedelta(days=90),
                idle_expires_at=now + timedelta(days=30),
            )
        )
        archive_db.add(
            DeviceToken(
                id=device_token_id,
                owner_id=7,
                device_id="laptop",
                token_hash="d" * 64,
                created_at=now,
            )
        )
        archive_db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="laptop",
                started_at=now,
                last_activity_at=now,
                summary_title="Before",
                primary_thread_id=thread_id,
            )
        )
        archive_db.flush()
        archive_db.add(
            TimelineCard(
                session_id=session_id,
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="laptop",
                started_at=now,
                last_activity_at=now,
                summary_title="Before",
                parser_revision="test-parser",
            )
        )
        archive_db.add(
            SessionThread(
                id=thread_id,
                session_id=session_id,
                provider="codex",
                is_primary=1,
                created_at=now,
                updated_at=now,
            )
        )
        archive_db.flush()
        archive_db.add(
            SessionThreadAlias(
                id=13,
                thread_id=thread_id,
                provider="codex",
                alias_kind="provider_session_id",
                alias_value="provider-session-1",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        archive_db.add(
            SessionRun(
                id=run_id,
                thread_id=thread_id,
                provider="codex",
                host_id="laptop",
                argv_redacted_json=["codex"],
                started_at=now,
            )
        )
        archive_db.flush()
        archive_db.add(
            SessionConnection(
                id=17,
                run_id=run_id,
                control_plane="codex_bridge",
                acquisition_kind="spawned_control",
                state="attached",
                device_id="laptop",
                can_send_input=1,
                capabilities_extra_json={"approval": True},
                acquired_at=now,
            )
        )
        archive_db.add(
            SessionLaunchAttempt(
                id=19,
                session_id=session_id,
                thread_id=thread_id,
                run_id=run_id,
                provider="codex",
                host_id="laptop",
                owner_id=7,
                client_request_id="request-1",
                state="adopted",
                created_at=now,
                updated_at=now,
            )
        )
        archive_db.commit()

        with LiveSession() as live_db:
            first = backfill_live_catalog(archive_db, live_db, batch_size=2)
            assert first.total == 13

        session = archive_db.get(AgentSession, session_id)
        card = archive_db.get(TimelineCard, session_id)
        assert session is not None
        assert card is not None
        session.summary_title = "After"
        card.summary_title = "After"
        archive_db.commit()

        with LiveSession() as live_db:
            assert sync_live_catalog_session(archive_db, live_db, session_id=session_id) is True
            assert live_db.query(LiveSessionCatalog).one().summary_title == "After"
            assert live_db.query(LiveTimelineCard).one().summary_title == "After"
            second = backfill_live_catalog(archive_db, live_db, batch_size=3)
            assert second.as_dict() == first.as_dict()
            assert live_db.query(LiveUser).count() == 1
            assert live_db.query(LiveDeviceToken).count() == 1
            # Auth/device code intentionally reuses the mature archive ORM
            # mappings against compatible physical tables in the live DB.
            assert live_db.query(User).one().email == "catalog@example.com"
            assert live_db.query(RefreshSession).one().user_id == 7
            assert live_db.query(DeviceToken).one().device_id == "laptop"
            assert live_db.query(LiveSessionCatalog).one().summary_title == "After"
            assert live_db.query(LiveTimelineCard).one().summary_title == "After"
            assert live_db.query(LiveSessionThread).count() == 1
            assert live_db.query(LiveSessionThreadAlias).count() == 1
            assert live_db.query(LiveSessionRun).count() == 1
            assert live_db.query(LiveSessionConnection).one().can_send_input == 1
            assert live_db.query(LiveSessionLaunchAttempt).one().state == "adopted"


def test_live_catalog_schema_inventory_is_created(tmp_path):
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(live_engine)
    with live_engine.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert set(live_catalog_table_names()).issubset(tables)


def test_live_catalog_sync_repairs_legacy_null_with_live_default(tmp_path):
    archive_engine = make_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(archive_engine)
    initialize_live_database(live_engine)
    ArchiveSession = make_sessionmaker(archive_engine)
    LiveSession = make_sessionmaker(live_engine)
    now = datetime.now(timezone.utc)
    session_id = uuid4()

    with ArchiveSession() as archive_db:
        archive_db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                environment="production",
                started_at=now,
                last_activity_at=now,
            )
        )
        archive_db.commit()

        # Production archives can contain NULLs from schemas that predate the
        # current NOT NULL constraint.  Model that legacy row in the identity
        # map without trying to write it back into this fresh archive schema.
        legacy_session = archive_db.get(AgentSession, session_id)
        assert legacy_session is not None
        archive_db.autoflush = False
        legacy_session.user_state = None

        with LiveSession() as live_db:
            assert sync_live_catalog_session(archive_db, live_db, session_id=session_id) is True
            assert live_db.get(LiveSessionCatalog, str(session_id)).user_state == "active"

            result = backfill_live_catalog(archive_db, live_db, batch_size=1)
            assert result.sessions == 1
            assert live_db.get(LiveSessionCatalog, str(session_id)).user_state == "active"
