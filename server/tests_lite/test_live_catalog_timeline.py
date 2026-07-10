from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest

from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
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
                archive_state="current",
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
                timeline_anchor_at=now,
                runtime_version=4,
                updated_at=now,
            )
        )
        db.commit()

        response = list_live_catalog_timeline(db, params=_params())

    assert response.total == 1
    assert response.has_real_sessions is True
    [card] = response.sessions
    assert card.thread_id == str(thread_id)
    assert card.head.id == str(session_id)
    assert card.head.timeline_title == "Storage isolation"
    assert card.head.runtime_phase == "thinking"
    assert card.head.user_messages == 1
    assert card.head.capabilities.staleness_reason is None


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
