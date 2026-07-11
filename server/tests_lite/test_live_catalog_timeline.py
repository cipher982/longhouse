from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest

from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard
from zerg.services.live_catalog_timeline import list_live_catalog_sessions
from zerg.services.live_catalog_timeline import list_live_catalog_timeline
from zerg.services.timeline_session_listing import TimelineSessionListParams


def _params(**overrides):
    values = {
        "project": None,
        "provider": None,
        "environment": None,
        "include_test": False,
        "hide_autonomous": True,
        "include_automation": False,
        "device_id": None,
        "days_back": 14,
        "query": None,
        "limit": 20,
        "offset": 0,
        "sort": None,
        "mode": "lexical",
        "context_mode": "forensic",
    }
    values.update(overrides)
    return TimelineSessionListParams(**values)


def _add_live_kernel(
    db,
    *,
    session_id,
    thread_id,
    now,
    provider="codex",
    control_plane="codex_app_server",
    acquisition_kind="spawned_control",
    connection_state="attached",
    can_send=1,
    can_tail=1,
    can_resume=1,
):
    run_id = str(uuid4())
    db.add(
        LiveSessionThread(
            id=str(thread_id),
            session_id=str(session_id),
            provider=provider,
            branch_kind="root",
            is_primary=1,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        LiveSessionRun(
            id=run_id,
            thread_id=str(thread_id),
            provider=provider,
            launch_origin="longhouse_spawned" if acquisition_kind != "observe_only" else "external_adopted",
            started_at=now,
        )
    )
    db.add(
        LiveSessionConnection(
            run_id=run_id,
            control_plane=control_plane,
            acquisition_kind=acquisition_kind,
            state=connection_state,
            can_send_input=can_send,
            can_interrupt=can_send,
            can_terminate=can_send,
            can_tail_output=can_tail,
            can_resume=can_resume,
            acquired_at=now,
            last_health_at=now,
        )
    )


def test_live_catalog_timeline_lists_card_and_runtime_without_archive(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    LiveSession = make_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    thread_id = uuid4()
    with LiveSession() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                device_name="Cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                assistant_messages=2,
                tool_calls=3,
                summary_title="Storage isolation",
                primary_thread_id=str(thread_id),
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveTimelineCard(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                summary_title="Storage isolation",
                first_user_message_preview="Fix the database",
                user_messages=1,
                assistant_messages=2,
                tool_calls=3,
                archive_state="legacy_hot",
                derived_state="current",
                parser_revision="test",
                updated_at=now,
            )
        )
        db.add(
            LiveRuntimeState(
                runtime_key="codex:test",
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                phase="thinking",
                phase_source="bridge",
                last_runtime_signal_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=now + timedelta(minutes=1),
                runtime_version=4,
                updated_at=now,
            )
        )
        _add_live_kernel(db, session_id=session_id, thread_id=thread_id, now=now)
        db.commit()

        response = list_live_catalog_timeline(db, params=_params())

    assert response.total == 1
    assert response.has_real_sessions is True
    [card] = response.sessions
    assert card.thread_id == str(thread_id)
    assert card.head.id == str(session_id)
    assert card.head.timeline_title == "Storage isolation"
    assert card.head.runtime_phase == "thinking"
    assert card.head.runtime_display.state == "thinking"
    assert card.head.timeline_card.status.label == "Thinking"
    assert card.head.user_messages == 1
    assert card.head.capabilities.staleness_reason is None
    assert card.head.capabilities.control_label == "live"
    assert card.head.capabilities.observe_only is False
    assert card.head.capabilities.can_send_input is True


def test_live_catalog_timeline_keeps_runtime_and_control_axes_independent(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'truth-table.db'}")
    initialize_live_database(engine)
    LiveSession = make_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    cases = {
        "Managed idle": (uuid4(), uuid4(), "opencode"),
        "Imported history": (uuid4(), uuid4(), "codex"),
        "Shadow active": (uuid4(), uuid4(), "claude"),
    }

    with LiveSession() as db:
        for title, (session_id, thread_id, provider) in cases.items():
            db.add(
                LiveSessionCatalog(
                    session_id=str(session_id),
                    provider=provider,
                    environment="production",
                    project="longhouse",
                    device_id="cinder",
                    device_name="Cinder",
                    started_at=now,
                    last_activity_at=now,
                    user_messages=1,
                    assistant_messages=1,
                    summary_title=title,
                    primary_thread_id=str(thread_id),
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                LiveTimelineCard(
                    session_id=str(session_id),
                    provider=provider,
                    environment="production",
                    project="longhouse",
                    device_id="cinder",
                    started_at=now,
                    last_activity_at=now,
                    summary_title=title,
                    user_messages=1,
                    assistant_messages=1,
                    archive_state="legacy_hot",
                    derived_state="current",
                    parser_revision="test",
                    updated_at=now,
                )
            )

        managed_id, managed_thread_id, _ = cases["Managed idle"]
        _add_live_kernel(
            db,
            session_id=managed_id,
            thread_id=managed_thread_id,
            now=now,
            provider="opencode",
            control_plane="opencode_server_bridge",
            connection_state="detached",
        )
        db.add(
            LiveRuntimeState(
                runtime_key="opencode:managed-idle",
                session_id=managed_id,
                provider="opencode",
                device_id="cinder",
                phase="idle",
                phase_source="bridge",
                last_runtime_signal_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=now + timedelta(minutes=1),
                runtime_version=1,
                updated_at=now,
            )
        )

        shadow_id, shadow_thread_id, _ = cases["Shadow active"]
        _add_live_kernel(
            db,
            session_id=shadow_id,
            thread_id=shadow_thread_id,
            now=now,
            provider="claude",
            control_plane="log_tail",
            acquisition_kind="observe_only",
            can_send=0,
            can_tail=1,
            can_resume=0,
        )
        db.add(
            LiveRuntimeState(
                runtime_key="claude:shadow-active",
                session_id=shadow_id,
                provider="claude",
                device_id="cinder",
                phase="thinking",
                phase_source="hook",
                last_runtime_signal_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=now + timedelta(minutes=1),
                runtime_version=1,
                updated_at=now,
            )
        )
        db.commit()
        response = list_live_catalog_timeline(db, params=_params())

    by_title = {card.head.timeline_title: card.head for card in response.sessions}

    managed = by_title["Managed idle"]
    assert managed.runtime_display.state == "idle"
    assert managed.timeline_card.status.label == "Idle"
    assert managed.capabilities.control_label == "reattach"
    assert managed.capabilities.observe_only is False
    assert managed.capabilities.can_resume is True

    imported = by_title["Imported history"]
    assert imported.runtime_display.state is None
    assert imported.timeline_card.status.label == "No live signal"
    assert imported.capabilities.control_label == "imported"
    assert imported.capabilities.observe_only is False
    assert imported.capabilities.search_only is True

    shadow = by_title["Shadow active"]
    assert shadow.runtime_display.state == "thinking"
    assert shadow.timeline_card.status.label == "Thinking"
    assert shadow.capabilities.control_label == "search-only"
    assert shadow.capabilities.observe_only is True
    assert shadow.capabilities.can_tail_output is True
    assert shadow.capabilities.can_send_input is False


def test_live_catalog_timeline_does_not_keep_stale_adopted_shell_working(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'stale-pending.db'}")
    initialize_live_database(engine)
    LiveSession = make_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    stale_at = now - timedelta(days=1)
    session_id = uuid4()

    with LiveSession() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="zerg",
                device_id="cinder",
                started_at=stale_at,
                last_activity_at=stale_at,
                user_messages=1,
                created_at=stale_at,
                updated_at=stale_at,
            )
        )
        db.add(
            LiveTimelineCard(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="zerg",
                device_id="cinder",
                started_at=stale_at,
                last_activity_at=stale_at,
                user_messages=1,
                archive_state="pending",
                derived_state="current",
                parser_revision="test",
                updated_at=stale_at,
            )
        )
        db.add(
            LiveLaunchReadiness(
                session_id=str(session_id),
                provider="codex",
                device_id="cinder",
                execution_lifetime="live_control",
                state="adopted",
                created_at=stale_at,
                updated_at=stale_at,
            )
        )
        db.commit()
        response = list_live_catalog_timeline(db, params=_params())

    [card] = response.sessions
    assert card.head.runtime_display.state is None
    assert card.head.timeline_card.status.label == "No live signal"
    assert card.head.capabilities.control_label == "imported"
    assert card.head.capabilities.observe_only is False
    assert card.head.capabilities.staleness_reason == "imported_only"


def test_live_catalog_timeline_returns_typed_archive_requirement_for_search(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    LiveSession = make_sessionmaker(engine)
    with LiveSession() as db, pytest.raises(ValueError, match="search_requires_archive"):
        list_live_catalog_timeline(db, params=_params(query="sqlite"))


def test_live_catalog_machine_list_reuses_bounded_projection(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    LiveSession = make_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    with LiveSession() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
            )
        )
        db.add(
            LiveTimelineCard(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                archive_state="current",
                derived_state="current",
                parser_revision="test",
            )
        )
        db.commit()
        response = list_live_catalog_sessions(db, params=_params())

    assert response.total == 1
    assert response.sessions[0].id == str(session_id)
