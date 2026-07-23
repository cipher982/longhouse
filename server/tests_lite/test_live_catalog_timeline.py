from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import zerg.routers.timeline as timeline_router
import zerg.services.live_catalog_timeline as live_catalog_timeline
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.live_catalog_timeline import list_live_catalog_timeline
from zerg.services.live_catalog_timeline import project_catalog_session_facts
from zerg.services.live_catalog_timeline import project_catalog_sessions_snapshot
from zerg.services.live_catalog_timeline import project_catalog_timeline_snapshot
from zerg.services.live_catalog_timeline import read_live_catalog_session
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


def _snapshot(db, params: TimelineSessionListParams):
    return CatalogStore(db.get_bind()).list_session_timeline(
        project=params.project,
        provider=params.provider,
        environment=params.environment,
        include_test=params.include_test,
        hide_autonomous=params.hide_autonomous,
        include_automation=params.include_automation,
        device_id=params.device_id,
        days_back=params.days_back,
        limit=params.limit,
        offset=params.offset,
    )


def test_detail_read_has_no_legacy_serve_kill_switch(monkeypatch):
    session_id = str(uuid4())
    projected = object()
    canonical_snapshot = {
        "found": True,
        "commit_seq": "7",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "legacy_facts": {"session": {"session_id": session_id}},
        "heads": [],
        "heads_truncated": False,
    }
    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_DETAIL_SERVE", "legacy")
    monkeypatch.setattr(
        live_catalog_timeline,
        "shadow_session_state_snapshot",
        lambda *_args, **_kwargs: canonical_snapshot,
    )

    def project(facts, *, observed_at, canonical_heads=None, commit_seq=None):
        assert facts is canonical_snapshot["legacy_facts"]
        assert observed_at.tzinfo is not None
        assert canonical_heads == []
        assert commit_seq == 7
        return projected

    monkeypatch.setattr(live_catalog_timeline, "project_catalog_session_facts", project)

    result, provider_alias, commit_seq = read_live_catalog_session(session_id, owner_id=3)

    assert result is projected
    assert provider_alias is None
    assert commit_seq == "7"


def test_canonical_host_projection_preserves_same_snapshot_liveness():
    observed_at = datetime.now(timezone.utc)
    host = live_catalog_timeline._host_facts(
        {"machine_heartbeat": {"received_at": observed_at.isoformat(), "is_offline": False}},
        now=observed_at,
    )

    assert host.state == "online"
    assert host.observed_at == observed_at


def test_canonical_detail_projects_one_owner_scoped_snapshot(monkeypatch):
    session_id = str(uuid4())
    projected = object()
    heads = [{"family": "activity"}]
    canonical_snapshot = {
        "found": True,
        "commit_seq": "19",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "legacy_facts": {"session": {"session_id": session_id}, "provider_alias": "thread-1"},
        "heads": heads,
        "heads_truncated": False,
    }

    def shadow(requested, *, owner_id):
        assert requested == session_id
        assert owner_id == 3
        return canonical_snapshot

    def project(facts, *, observed_at, canonical_heads=None, commit_seq=None):
        assert facts is canonical_snapshot["legacy_facts"]
        assert canonical_heads is heads
        assert commit_seq == 19
        return projected

    monkeypatch.setattr(live_catalog_timeline, "shadow_session_state_snapshot", shadow)
    monkeypatch.setattr(live_catalog_timeline, "project_catalog_session_facts", project)

    result, provider_alias, commit_seq = read_live_catalog_session(session_id, owner_id=3)

    assert result is projected
    assert provider_alias == "thread-1"
    assert commit_seq == "19"


@pytest.mark.parametrize(
    ("snapshot_update", "expected_code"),
    [
        ({"heads_truncated": True}, "shadow_fact_head_limit_exceeded"),
        ({"heads": None}, "invalid_catalog_snapshot"),
        ({"legacy_facts": None}, "invalid_catalog_snapshot"),
    ],
)
def test_canonical_detail_fails_closed_on_incomplete_snapshot(monkeypatch, snapshot_update, expected_code):
    session_id = str(uuid4())
    snapshot = {
        "found": True,
        "commit_seq": "23",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "legacy_facts": {"session": {"session_id": session_id}},
        "heads": [],
        "heads_truncated": False,
        **snapshot_update,
    }
    monkeypatch.setattr(live_catalog_timeline, "shadow_session_state_snapshot", lambda *_args, **_kwargs: snapshot)
    with pytest.raises(CatalogReadError) as raised:
        read_live_catalog_session(session_id, owner_id=3)

    assert raised.value.code == expected_code


def test_canonical_detail_requires_owner_scope_before_catalog_read(monkeypatch):
    monkeypatch.setattr(
        live_catalog_timeline,
        "shadow_session_state_snapshot",
        lambda *_args, **_kwargs: pytest.fail("unscoped canonical detail must not read catalog state"),
    )

    with pytest.raises(CatalogReadError) as raised:
        read_live_catalog_session(uuid4())

    assert raised.value.code == "canonical_owner_required"


@pytest.mark.asyncio
async def test_storage_v2_browser_search_hydrates_hits_with_owner_scope(monkeypatch):
    session_id = uuid4()
    observed: dict[str, object] = {}

    class SearchClient:
        async def call(self, method, params):
            assert method == "search.query.v2"
            assert params["owner_id"] == "7"
            return {"results": [{"session_id": str(session_id)}]}

    def read_session(requested, *, owner_id):
        observed.update(requested=requested, owner_id=owner_id)
        return None, None, "9"

    monkeypatch.setattr(timeline_router, "get_searchd_client", lambda: SearchClient())
    monkeypatch.setattr(timeline_router, "read_live_catalog_session", read_session)

    result = await timeline_router._search_storage_v2_timeline(
        owner_id=7,
        params=_params(query="needle"),
    )

    assert result.total == 0
    assert observed == {"requested": session_id, "owner_id": 7}


def test_canonical_timeline_projects_all_rows_at_snapshot_commit(monkeypatch):
    projected = SimpleNamespace(
        id=str(uuid4()),
        timeline_anchor_at=datetime.now(timezone.utc),
        origin_label="prod",
        environment="prod",
    )
    heads = [{"family": "activity"}]
    snapshot = {
        "commit_seq": "31",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "rows": [{"thread_id": projected.id, "facts": {"catalog": {}}, "heads": heads, "heads_truncated": False}],
        "total": 1,
        "has_real_sessions": True,
    }
    captured = {}

    def canonical(params, *, owner_id):
        captured.update(params=params, owner_id=owner_id)
        return snapshot

    def project(facts, *, observed_at, canonical_heads=None, commit_seq=None):
        assert facts is snapshot["rows"][0]["facts"]
        assert canonical_heads is heads
        assert commit_seq == 31
        return projected

    monkeypatch.setattr(live_catalog_timeline, "canonical_timeline_snapshot", canonical)
    monkeypatch.setattr(live_catalog_timeline, "project_catalog_session_facts", project)
    monkeypatch.setattr(
        live_catalog_timeline,
        "TimelineSessionCardResponse",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        live_catalog_timeline,
        "TimelineSessionsListResponse",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    result = list_live_catalog_timeline(params=_params(), owner_id=7)

    assert result.total == 1
    assert captured["owner_id"] == 7
    assert captured["params"]["limit"] == 20


def test_canonical_timeline_fails_closed_on_truncated_heads():
    snapshot = {
        "commit_seq": "31",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "rows": [{"facts": {}, "heads": [], "heads_truncated": True}],
        "total": 1,
        "has_real_sessions": True,
    }

    with pytest.raises(CatalogReadError) as raised:
        project_catalog_timeline_snapshot(snapshot)

    assert raised.value.code == "shadow_fact_head_limit_exceeded"


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
    initialize_catalog_schema(engine)
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

        response = project_catalog_timeline_snapshot(_snapshot(db, _params()))

    assert response.total == 1
    assert response.has_real_sessions is True
    [card] = response.sessions
    assert card.thread_id == str(thread_id)
    assert card.head.id == str(session_id)
    assert card.head.timeline_title == "Storage isolation"
    assert card.head.runtime_phase is None
    assert card.head.runtime_display.state is None
    assert card.head.timeline_card.status.label == "Activity unknown"
    assert card.head.user_messages == 1
    assert card.head.capabilities.staleness_reason == "control_unknown"
    assert card.head.session_state.mode == "helm"
    assert card.head.capabilities.observe_only is False
    assert card.head.session_state.control.actions.send_input.state == "unavailable"
    assert card.head.session_state.control.actions.resume.state == "available"


def test_live_catalog_timeline_labels_zero_content_shell_as_empty(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'empty-shell.db'}")
    initialize_catalog_schema(engine)
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
                started_at=now - timedelta(days=3),
                last_activity_at=now,
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                hidden_from_default_timeline=1,
                launch_actor="human_ui",
                launch_surface="ios",
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
                started_at=now - timedelta(days=3),
                last_activity_at=now,
                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                hidden_from_default_timeline=1,
                archive_state="legacy_hot",
                launch_actor="human_ui",
                launch_surface="ios",
                derived_state="current",
                parser_revision="test",
                updated_at=now,
            )
        )
        db.commit()

        response = project_catalog_timeline_snapshot(_snapshot(db, _params()))

    assert response.total == 0
    facts = CatalogStore(engine).read_session(session_id=str(session_id), owner_id=None)["facts"]
    session = project_catalog_session_facts(facts, observed_at=now)
    assert session.timeline_title == "longhouse · Empty session"
    assert session.title_state == "awaiting_input"
    assert session.title_source == "project"


def test_user_hide_updates_legacy_live_timeline_card_projection(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'user-hide-card.db'}")
    initialize_catalog_schema(engine)
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
                started_at=now,
                last_activity_at=now,
                user_messages=1,
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
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                archive_state="legacy_hot",
                derived_state="current",
                parser_revision="test",
                updated_at=now,
            )
        )
        db.commit()

    store = CatalogStore(engine)
    hidden = store.update_session_preferences(
        session_id=str(session_id),
        user_state=None,
        loop_mode=None,
        notification_muted=None,
        user_hidden_from_timeline=True,
        observed_at=now,
    )
    assert hidden["preferences"]["user_hidden_from_timeline"] is True
    with LiveSession() as db:
        assert _snapshot(db, _params())["total"] == 0

    restored = store.update_session_preferences(
        session_id=str(session_id),
        user_state=None,
        loop_mode=None,
        notification_muted=None,
        user_hidden_from_timeline=False,
        observed_at=now + timedelta(seconds=1),
    )
    assert restored["preferences"]["user_hidden_from_timeline"] is False
    with LiveSession() as db:
        assert _snapshot(db, _params())["total"] == 1


def test_live_catalog_question_preserves_opened_at_and_needs_answer(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live-question.db'}")
    initialize_catalog_schema(engine)
    LiveSession = make_sessionmaker(engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    opened_at = now - timedelta(seconds=20)
    session_id = uuid4()
    interaction_id = uuid4()
    with LiveSession() as db:
        db.add_all(
            [
                LiveSessionCatalog(
                    session_id=str(session_id),
                    provider="codex",
                    environment="production",
                    project="longhouse",
                    device_id="cinder",
                    started_at=now - timedelta(minutes=2),
                    last_activity_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                LiveTimelineCard(
                    session_id=str(session_id),
                    provider="codex",
                    environment="production",
                    project="longhouse",
                    device_id="cinder",
                    started_at=now - timedelta(minutes=2),
                    last_activity_at=now,
                    archive_state="pending",
                    derived_state="current",
                    parser_revision="test",
                    updated_at=now,
                ),
                LiveInteractionRequest(
                    id=str(interaction_id),
                    session_id=str(session_id),
                    runtime_key=f"codex:{session_id}",
                    provider="codex",
                    request_key=f"codex:{session_id}:question-1",
                    kind="structured_question",
                    status="pending",
                    can_respond=1,
                    projection_json={"summary": "Choose a migration path"},
                    occurred_at=opened_at,
                    last_seen_at=now,
                    expires_at=now + timedelta(minutes=5),
                ),
            ]
        )
        db.commit()

    snapshot = CatalogStore(engine).read_session(session_id=str(session_id))
    response = project_catalog_session_facts(
        snapshot["facts"],
        observed_at=datetime.fromisoformat(snapshot["observed_at"]),
    )

    pending = response.session_state.pending_interaction
    assert pending is not None
    assert pending.id == str(interaction_id)
    assert pending.kind == "question"
    assert pending.opened_at == opened_at
    assert response.session_state.presentation.primary is not None
    assert response.session_state.presentation.primary.key == "needs_answer"
    assert response.session_state.presentation.primary.observed_at == opened_at


def test_storage_v2_untitled_session_uses_first_prompt_as_pending_fallback(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'storage-title.db'}")
    initialize_catalog_schema(engine)
    LiveSession = make_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    with LiveSession() as db:
        db.add(
            StorageSession(
                session_id=str(session_id),
                tenant_id="tenant-a",
                owner_id="1",
                provider="codex",
                environment="production",
                machine_id="cinder",
                project="longhouse",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                first_user_message_preview="[Image #1]\n\nRepair storage titles without an AI queue",
                transcript_revision=1,
                raw_state="durable",
                render_state="ready",
                commit_seq=1,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

        response = project_catalog_timeline_snapshot(_snapshot(db, _params()))

    [card] = response.sessions
    assert card.head.timeline_title == "Repair storage titles without an AI…"
    assert card.head.summary_title == "Repair storage titles without an AI…"
    assert card.head.anchor_title is None
    assert card.head.title_state == "pending"
    assert card.head.title_source == "prompt"


def test_live_catalog_timeline_keeps_runtime_and_control_axes_independent(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'truth-table.db'}")
    initialize_catalog_schema(engine)
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
        response = project_catalog_timeline_snapshot(_snapshot(db, _params()))

    by_title = {card.head.timeline_title: card.head for card in response.sessions}

    managed = by_title["Managed idle"]
    assert managed.runtime_display.state is None
    assert managed.timeline_card.status.label == "Activity unknown"
    assert managed.session_state.mode == "helm"
    assert managed.capabilities.observe_only is False
    assert managed.capabilities.can_resume is True

    imported = by_title["Imported history"]
    assert imported.runtime_display.state is None
    assert imported.timeline_card.status.label == "Activity unknown"
    assert imported.session_state.mode == "shadow"
    assert imported.capabilities.observe_only is False
    assert imported.capabilities.search_only is True

    shadow = by_title["Shadow active"]
    assert shadow.runtime_display.state is None
    assert shadow.timeline_card.status.label == "Activity unknown"
    assert shadow.session_state.mode == "shadow"
    assert shadow.capabilities.observe_only is True
    assert shadow.capabilities.can_tail_output is True
    assert shadow.capabilities.can_send_input is False


def test_live_catalog_timeline_does_not_keep_stale_adopted_shell_working(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'stale-pending.db'}")
    initialize_catalog_schema(engine)
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
        response = project_catalog_timeline_snapshot(_snapshot(db, _params()))

    [card] = response.sessions
    assert card.head.runtime_display.state is None
    assert card.head.timeline_card.status.label == "Activity unknown"
    assert card.head.capabilities.control_label == "imported"
    assert card.head.capabilities.observe_only is False
    assert card.head.capabilities.staleness_reason == "control_unknown"


def test_live_catalog_timeline_returns_typed_archive_requirement_for_search(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_catalog_schema(engine)
    with pytest.raises(ValueError, match="search_requires_archive"):
        list_live_catalog_timeline(params=_params(query="sqlite"))


def test_live_catalog_machine_list_reuses_bounded_projection(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_catalog_schema(engine)
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
        response = project_catalog_sessions_snapshot(_snapshot(db, _params()))

        assert response.total == 1
        assert response.sessions[0].id == str(session_id)


def test_catalog_only_pending_session_projects_without_a_timeline_card(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'pending.db'}")
    initialize_catalog_schema(engine)
    LiveSession = make_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    with LiveSession() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                device_id="cinder",
                started_at=now,
            )
        )
        db.add(
            LiveLaunchReadiness(
                session_id=str(session_id),
                owner_id="1",
                provider="codex",
                device_id="cinder",
                execution_lifetime="live_control",
                state="pending",
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

    snapshot = CatalogStore(engine).read_session(session_id=str(session_id))
    response = project_catalog_session_facts(
        snapshot["facts"],
        observed_at=datetime.fromisoformat(snapshot["observed_at"]),
    )

    assert response.id == str(session_id)
    assert response.launch_state == "launching"
    assert response.user_messages == 0
