from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.database as database_module
from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.catalogd.models import CatalogBase
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionThreadAlias
from zerg.models.agents import SessionTurn
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession as LiveSessionRow
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveTimelineCard
from zerg.services.live_archive_outbox import HEARTBEAT_STAMP_KIND
from zerg.services.live_archive_outbox import MANAGED_LOCAL_LAUNCH_KIND
from zerg.services.live_archive_outbox import RUNTIME_EVENT_KIND
from zerg.services.live_archive_outbox import SESSION_INPUT_RECEIPT_KIND
from zerg.services.live_archive_outbox import drain_live_archive_outbox
from zerg.services.live_archive_outbox import enqueue_heartbeat_stamp_outbox
from zerg.services.live_archive_outbox import enqueue_managed_local_launch_outbox
from zerg.services.live_archive_outbox import enqueue_runtime_events_outbox
from zerg.services.live_archive_outbox import enqueue_session_input_receipt_outbox
from zerg.services.live_catalog_launch import create_live_launch_catalog_shell
from zerg.services.live_catalog_timeline import project_catalog_timeline_snapshot
from zerg.services.live_launch_readiness import get_live_launch_readiness_by_client_request
from zerg.services.live_launch_readiness import get_live_launch_readiness_by_session_id
from zerg.services.live_launch_readiness import latest_live_launch_readiness_map
from zerg.services.live_launch_readiness import reap_expired_live_launch_readiness
from zerg.services.live_launch_readiness import update_live_launch_readiness_state
from zerg.services.live_launch_readiness import upsert_live_launch_readiness
from zerg.services.live_session_inputs import upsert_live_input_receipt
from zerg.services.live_session_state import list_active_live_session_ids
from zerg.services.live_session_state import mark_missing_live_sessions
from zerg.services.live_session_state import upsert_live_sessions_from_managed_leases
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.managed_control_state import upsert_live_control_leases
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_live_runtime_events
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.session_runtime import session_input_block_reason
from zerg.services.session_runtime import session_is_closed_for_input
from zerg.services.session_views import latest_live_launch_readiness
from zerg.services.session_workspace import build_session_workspace
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.write_serializer import get_live_write_serializer
from zerg.services.write_serializer import get_write_serializer
from zerg.utils.time import normalize_utc


@pytest.fixture()
def live():
    """A Runtime Host's live catalog: catalogd up, ``live_catalog_enabled()`` true.

    The hot-lane tests below used to reach the Live Store by patching
    ``live_store_configured`` to true, which put them on the SQLAlchemy branch
    a Runtime Host never takes -- that same flag now also makes
    ``live_catalog_enabled()`` true, and the read goes to catalogd. So they run
    against a real daemon instead.
    """

    with provision_live_catalog() as catalog:
        yield catalog


def _runtime_event(
    *,
    session_id,
    kind: str,
    source: str,
    occurred_at: datetime,
    dedupe_key: str,
    provider: str = "codex",
    device_id: str = "cinder",
    phase: str | None = None,
    tool_name: str | None = None,
    freshness_ms: int | None = None,
    payload: dict | None = None,
) -> dict:
    """One wire runtime event, shaped the way the Machine Agent ships it."""

    return {
        "runtime_key": runtime_key_for_session(provider, str(session_id)),
        "session_id": str(session_id),
        "provider": provider,
        "device_id": device_id,
        "source": source,
        "kind": kind,
        "phase": phase,
        "tool_name": tool_name,
        "occurred_at": occurred_at.isoformat(),
        "freshness_ms": freshness_ms,
        "dedupe_key": dedupe_key,
        "payload": payload or {},
    }


def _machine_heartbeat(*, managed_sessions: list[dict]) -> dict:
    """One wire heartbeat. Sending ``managed_sessions`` makes the snapshot authoritative."""

    return {
        "version": "0.5.0",
        "daemon_pid": 12345,
        "spool_pending_count": 0,
        "parse_error_count_1h": 0,
        "consecutive_ship_failures": 0,
        "disk_free_bytes": 50_000_000_000,
        "is_offline": False,
        "managed_sessions": managed_sessions,
    }


def _managed_lease(*, session_id, sequence: int, provider: str = "codex", state: str = "attached") -> dict:
    return {
        "session_id": str(session_id),
        "provider": provider,
        "machine_id": "cinder",
        "state": state,
        "phase": "idle",
        "bridge_status": "ready",
        "thread_subscription_status": "active",
        "lease_ttl_ms": 60_000,
        "sequence": sequence,
    }


def _local_launch_rpc(plan, *, owner_id: int, started_at: datetime, expires_at: datetime) -> dict:
    """The ``session.launch.local.create.v2`` body the managed-launch route sends."""

    return {
        "owner_id": int(owner_id),
        "git_repo": None,
        "git_branch": None,
        "started_at": started_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "plan": {
            "session_id": str(plan.session_id),
            "provider": plan.provider,
            "provider_session_id": plan.provider_session_id,
            "source_name": plan.source_name,
            "source_runner_id": plan.source_runner_id,
            "cwd": plan.cwd,
            "project": plan.project,
            "display_name": plan.display_name,
            "managed_session_name": plan.managed_session_name,
            "loop_mode": plan.loop_mode,
            "permission_mode": plan.permission_mode,
            "launch_actor": plan.launch_actor,
            "launch_surface": plan.launch_surface,
            "environment": plan.environment,
            "origin_kind": plan.origin_kind,
            "hidden_from_default_timeline": plan.hidden_from_default_timeline,
            "managed_transport": plan.managed_transport,
            "attach_command": plan.attach_command,
            "provider_config": plan.provider_config,
        },
    }


def test_live_write_serializer_is_distinct_from_archive_serializer():
    assert get_live_write_serializer() is not get_write_serializer()


def test_live_catalog_process_does_not_construct_retired_database_engine(monkeypatch):
    from types import SimpleNamespace

    from zerg.database import _default_database_enabled_for_process

    monkeypatch.delenv("TESTING", raising=False)
    settings = SimpleNamespace(
        database_url="sqlite:////data/longhouse.db",
        live_database_url="sqlite:////data/longhouse-live.db",
        testing=False,
    )

    assert _default_database_enabled_for_process(settings) is False
    settings.database_url = "sqlite:///file:/data/longhouse.db?mode=ro&uri=true"
    assert _default_database_enabled_for_process(settings) is True


def test_initialize_live_database_creates_only_live_tables(tmp_path):
    from zerg.services.live_catalog_projection import live_catalog_table_names

    engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")

    initialize_live_database(engine)

    tables = set(inspect(engine).get_table_names())
    assert tables == {
        "live_archive_outbox",
        "live_control_leases",
        "live_heartbeat_stamps",
        "live_interaction_requests",
        "live_launch_readiness",
        "live_machine_control_operations",
        "machine_presence",
        "live_runtime_state",
        "live_session_input_receipts",
        "live_session_input_attachments",
        "live_console_turns",
        "live_session_live_previews",
        "live_sessions",
        "directed_inputs",
        "runner_enroll_tokens",
        "runner_health_incidents",
        "runner_jobs",
        "runners",
        "notification_events",
    } | set(live_catalog_table_names())
    assert "sessions" not in tables
    assert "agent_heartbeats" not in tables
    assert "events" not in tables
    catalog_only = set(CatalogBase.metadata.tables)
    assert catalog_only.isdisjoint(tables)

    initialize_catalog_schema(engine)
    assert catalog_only.issubset(set(inspect(engine).get_table_names()))


def test_live_session_state_upserts_and_marks_missing(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(engine)
    LiveSession = sessionmaker(bind=engine)
    first_session_id = uuid4()
    second_session_id = uuid4()
    first_seen_at = datetime.now(timezone.utc)
    second_seen_at = first_seen_at + timedelta(seconds=1)
    missing_seen_at = second_seen_at + timedelta(seconds=1)

    try:
        with LiveSession() as live_db:
            touched = upsert_live_sessions_from_managed_leases(
                live_db,
                [
                    SimpleNamespace(
                        session_id=first_session_id,
                        provider="codex",
                        machine_id="cinder",
                        state="attached",
                        observed_at=first_seen_at,
                    ),
                    SimpleNamespace(
                        session_id=second_session_id,
                        provider="claude",
                        machine_id="cinder",
                        state="attached",
                        observed_at=first_seen_at,
                    ),
                ],
                device_id="cinder",
                owner_id=123,
                received_at=first_seen_at,
            )
            live_db.commit()
            assert touched == {first_session_id, second_session_id}

            touched = upsert_live_sessions_from_managed_leases(
                live_db,
                [
                    SimpleNamespace(
                        session_id=first_session_id,
                        provider="codex",
                        machine_id="cinder",
                        state="degraded",
                        observed_at=second_seen_at,
                    )
                ],
                device_id="cinder",
                owner_id=None,
                received_at=second_seen_at,
            )
            missing = mark_missing_live_sessions(
                live_db,
                touched,
                device_id="cinder",
                received_at=missing_seen_at,
            )
            live_db.commit()

            first = live_db.get(LiveSessionRow, str(first_session_id))
            second = live_db.get(LiveSessionRow, str(second_session_id))
            assert first is not None
            assert first.owner_id == "123"
            assert first.state == "degraded"
            assert normalize_utc(first.last_seen_at) == second_seen_at
            assert second is not None
            assert second.state == "missing"
            assert normalize_utc(second.last_seen_at) == first_seen_at
            assert normalize_utc(second.updated_at) == missing_seen_at
            assert missing == {second_session_id}
    finally:
        engine.dispose()


def test_live_heartbeat_stamp_extends_legacy_columns_with_bounded_receipt():
    archive_columns = {column.name for column in AgentHeartbeat.__table__.columns if column.name != "id"}
    live_columns = {column.name for column in LiveHeartbeatStamp.__table__.columns if column.name != "id"}

    assert live_columns == archive_columns | {"request_sha256", "catalog_result_json"}


def test_archive_and_live_runtime_state_columns_stay_in_sync():
    archive_columns = {column.name for column in SessionRuntimeState.__table__.columns}
    live_columns = {column.name for column in LiveRuntimeState.__table__.columns}

    assert live_columns == archive_columns


def test_hot_interaction_and_archive_state_match_list_and_workspace_before_archive_convergence(tmp_path, monkeypatch):
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    archive_engine = archive_engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=archive_engine)
    archive_factory = sessionmaker(bind=archive_engine)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    initialize_catalog_schema(live_engine)
    live_factory = sessionmaker(bind=live_engine)
    session_id = uuid4()
    pause_id = str(uuid4())
    now = datetime.now(timezone.utc)
    projection = {
        "id": pause_id,
        "session_id": str(session_id),
        "runtime_key": f"codex:{session_id}",
        "request_key": "codex:runtime:question",
        "status": "pending",
        "kind": "structured_question",
        "provider": "codex",
        "occurred_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "can_respond": True,
        "questions": [
            {
                "id": "choice",
                "question": "Choose",
                "options": [{"label": "fast"}, {"label": "safe"}],
            }
        ],
    }

    with archive_factory() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                environment="production",
                project="parity",
                started_at=now,
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
            )
        )
        db.commit()
    with live_factory() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="parity",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                assistant_messages=1,
            )
        )
        db.add(
            LiveTimelineCard(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="parity",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                assistant_messages=1,
                transcript_revision=7,
                archive_state="pending",
                parser_revision="test",
                updated_at=now,
            )
        )
        db.add(
            LiveRuntimeState(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                provider="codex",
                phase="needs_user",
                phase_source="provider",
                timeline_anchor_at=now,
                pending_interaction_id=pause_id,
                pending_interaction_kind="structured_question",
                pending_interaction_opened_at=now,
                pending_interaction_updated_at=now,
                pending_interaction_projection_json=projection,
                pending_interaction_can_respond=1,
                runtime_version=3,
                updated_at=now,
            )
        )
        db.add(
            LiveInteractionRequest(
                id=str(pause_id),
                session_id=str(session_id),
                runtime_key=f"codex:{session_id}",
                provider="codex",
                request_key="codex:runtime:question",
                kind="structured_question",
                status="pending",
                can_respond=1,
                request_payload_json={},
                projection_json=projection,
                occurred_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(database_module, "get_catalog_session_factory", lambda: live_factory)
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: live_factory)
    catalog_store = CatalogStore(live_engine)
    monkeypatch.setattr(
        "zerg.services.catalog_facts.session_batch_snapshot",
        lambda session_ids: catalog_store.read_sessions(session_ids=session_ids),
    )

    params = TimelineSessionListParams(
        project=None,
        provider=None,
        environment=None,
        include_test=True,
        hide_autonomous=False,
        device_id=None,
        days_back=30,
        query=None,
        limit=10,
        offset=0,
        sort=None,
        mode="lexical",
        context_mode="default",
        include_automation=True,
    )
    timeline = project_catalog_timeline_snapshot(
        catalog_store.list_session_timeline(
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
    )
    with archive_factory() as db:
        workspace = build_session_workspace(db=db, session_id=session_id)

    list_session = timeline.sessions[0].head
    assert workspace.session.runtime_display.pause_request == list_session.runtime_display.pause_request
    assert workspace.session.session_state.pending_interaction == list_session.session_state.pending_interaction
    assert workspace.session.session_state.transcript == list_session.session_state.transcript
    assert workspace.session.runtime_display.pause_request is not None
    assert workspace.session.runtime_display.pause_request.questions[0].question == "Choose"


def test_live_input_receipt_is_idempotent_by_client_request_id(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(engine)
    LiveSession = sessionmaker(bind=engine)
    session_id = uuid4()
    now = datetime.now(timezone.utc)

    try:
        with LiveSession() as live_db:
            first = upsert_live_input_receipt(
                live_db,
                owner_id=123,
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                client_request_id="client-1",
                text="ship it",
                intent="auto",
                status="queued",
                archive_session_input_id=41,
                now=now,
            )
            live_db.commit()
            first_id = first.id

        with LiveSession() as live_db:
            second = upsert_live_input_receipt(
                live_db,
                owner_id=123,
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                client_request_id="client-1",
                text="ship it",
                intent="auto",
                status="delivered",
                archive_session_input_id=41,
                now=now + timedelta(seconds=1),
            )
            live_db.commit()

            rows = live_db.query(LiveSessionInputReceipt).all()
            assert len(rows) == 1
            assert second.id == first_id
            assert rows[0].status == "delivered"
            assert rows[0].archive_session_input_id == 41
            assert rows[0].text == "ship it"
    finally:
        engine.dispose()


def test_live_archive_outbox_drains_heartbeat_to_archive_idempotently(tmp_path):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    heartbeat = {
        "device_id": "live-drain",
        "received_at": now,
        "version": "0.5.0",
        "last_ship_at": None,
        "last_ship_attempt_at": now,
        "last_ship_result": "ok",
        "last_ship_latency_ms": 123,
        "last_ship_http_status": 204,
        "spool_pending": 2,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 3,
        "ship_successes_1h": 3,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "ship_latency_p50_ms_1h": 100,
        "ship_latency_p95_ms_1h": 200,
        "disk_free_bytes": 123_456,
        "is_offline": 0,
        "raw_json": '{"ok":true}',
        "sessions_digest": "digest-live-drain",
        "sessions_sequence": 9,
    }

    try:
        with LiveSession() as live_db:
            assert enqueue_heartbeat_stamp_outbox(live_db, heartbeat) is True
            assert enqueue_heartbeat_stamp_outbox(live_db, heartbeat) is False
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10, now=now + timedelta(seconds=1))

        assert result.processed == 1
        assert result.drained == 1
        assert result.failed == 0

        with ArchiveSession() as archive_db:
            rows = archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain").all()
            assert len(rows) == 1
            assert rows[0].version == "0.5.0"
            assert rows[0].spool_pending == 2
            assert rows[0].disk_free_bytes == 123_456
            assert rows[0].raw_json == '{"ok":true}'
            assert rows[0].sessions_digest == "digest-live-drain"

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.kind == HEARTBEAT_STAMP_KIND
            assert row.drained_at is not None
            assert row.last_error is None
            assert row.attempts == 1

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain").count() == 1
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_drains_managed_local_launch_to_archive_idempotently(tmp_path):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=42,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="claude",
            project="demo",
            git_repo="git@example.com:demo/repo.git",
            git_branch="main",
            machine_name="cinder",
            native_claude_channels_available=True,
        )
    )

    try:
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=plan.session_id,
                owner_id=42,
                device_id=plan.source_name,
                provider=plan.provider,
                execution_lifetime="live_control",
                state="pending",
                command_id=f"managed-local-{plan.session_id}",
                client_request_id=None,
                machine_id=plan.source_name,
                project=plan.project,
                expires_at=now + timedelta(minutes=2),
                now=now,
            )
            assert (
                enqueue_managed_local_launch_outbox(
                    live_db,
                    plan=plan,
                    owner_id=42,
                    git_repo="git@example.com:demo/repo.git",
                    git_branch="main",
                    started_at=now,
                )
                is True
            )
            assert (
                enqueue_managed_local_launch_outbox(
                    live_db,
                    plan=plan,
                    owner_id=42,
                    git_repo="git@example.com:demo/repo.git",
                    git_branch="main",
                    started_at=now,
                )
                is False
            )
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10, now=now + timedelta(seconds=1))

        assert result.processed == 1
        assert result.drained == 1
        assert result.failed == 0

        with ArchiveSession() as archive_db:
            session = archive_db.get(AgentSession, plan.session_id)
            assert session is not None
            assert session.provider == "claude"
            assert session.device_id == "cinder"
            assert session.cwd == "/tmp/demo"
            assert session.git_repo == "git@example.com:demo/repo.git"
            assert session.git_branch == "main"
            assert normalize_utc(session.started_at) == now
            assert session.primary_thread_id is not None
            alias = archive_db.query(SessionThreadAlias).one()
            assert alias.alias_kind == "provider_session_id"
            assert alias.alias_value == plan.provider_session_id
            run = archive_db.query(SessionRun).one()
            assert run.cwd == "/tmp/demo"
            connection = archive_db.query(SessionConnection).one()
            assert connection.external_name == plan.managed_session_name
            assert connection.state == "detached"
            runtime = archive_db.query(SessionRuntimeState).one()
            assert runtime.session_id == plan.session_id
            assert runtime.phase == "idle"

        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).one()
            assert outbox.kind == MANAGED_LOCAL_LAUNCH_KIND
            assert outbox.drained_at is not None
            readiness = live_db.get(LiveLaunchReadiness, str(plan.session_id))
            assert readiness is not None
            assert readiness.state == "adopted"
            assert readiness.expires_at is None

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentSession).filter(AgentSession.id == plan.session_id).count() == 1
            assert archive_db.query(SessionRun).count() == 1
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_managed_local_launch_outbox_retries_after_live_mark_drained_commit_failure(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=42,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="codex",
            project="demo",
            git_repo="git@example.com:demo/repo.git",
            git_branch="main",
            machine_name="cinder",
        )
    )

    try:
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=plan.session_id,
                owner_id=42,
                device_id=plan.source_name,
                provider=plan.provider,
                execution_lifetime="live_control",
                state="pending",
                command_id=f"managed-local-{plan.session_id}",
                client_request_id=None,
                machine_id=plan.source_name,
                project=plan.project,
                expires_at=now + timedelta(minutes=2),
                now=now,
            )
            enqueue_managed_local_launch_outbox(
                live_db,
                plan=plan,
                owner_id=42,
                git_repo="git@example.com:demo/repo.git",
                git_branch="main",
                started_at=now,
            )
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:

            def fail_live_commit_once():
                raise RuntimeError("live commit failed")

            with monkeypatch.context() as commit_patch:
                commit_patch.setattr(live_db, "commit", fail_live_commit_once)
                result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 1
        assert result.drained == 0
        assert result.failed == 1
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentSession).filter(AgentSession.id == plan.session_id).count() == 1
            assert archive_db.query(SessionRun).count() == 1
        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).one()
            assert outbox.drained_at is None
            readiness = live_db.get(LiveLaunchReadiness, str(plan.session_id))
            assert readiness is not None
            assert readiness.state == "pending"

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 1
        assert result.drained == 1
        assert result.failed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentSession).filter(AgentSession.id == plan.session_id).count() == 1
            assert archive_db.query(SessionRun).count() == 1
        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).one()
            assert outbox.drained_at is not None
            readiness = live_db.get(LiveLaunchReadiness, str(plan.session_id))
            assert readiness is not None
            assert readiness.state == "adopted"
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_drains_session_input_receipt_to_archive(tmp_path):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="live-input-outbox",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

            from zerg.services.session_turns import create_session_turn

            create_session_turn(archive_db, session_id=session_id, request_id="req-live-input-1")
            archive_db.commit()

        with LiveSession() as live_db:
            receipt = upsert_live_input_receipt(
                live_db,
                owner_id=123,
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                client_request_id="client-live-input-1",
                text="project through outbox",
                intent="auto",
                status="delivered",
                delivery_request_id="req-live-input-1",
                now=now,
            )
            assert (
                enqueue_session_input_receipt_outbox(
                    live_db,
                    receipt_id=receipt.id,
                    owner_id=123,
                    session_id=session_id,
                    text="project through outbox",
                    intent="auto",
                    client_request_id="client-live-input-1",
                    delivery_request_id="req-live-input-1",
                )
                is True
            )
            assert (
                enqueue_session_input_receipt_outbox(
                    live_db,
                    receipt_id=receipt.id,
                    owner_id=123,
                    session_id=session_id,
                    text="project through outbox",
                    intent="auto",
                    client_request_id="client-live-input-1",
                    delivery_request_id="req-live-input-1",
                )
                is False
            )
            live_db.commit()
            receipt_id = receipt.id

        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionInput).filter(SessionInput.session_id == session_id).count() == 0

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10, now=now + timedelta(seconds=1))

        assert result.processed == 1
        assert result.drained == 1
        assert result.failed == 0

        with ArchiveSession() as archive_db:
            row = archive_db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == "delivered"
            assert row.client_request_id == "client-live-input-1"
            assert row.delivery_request_id == "req-live-input-1"
            assert row.body == "project through outbox"
            turn = archive_db.query(SessionTurn).filter(SessionTurn.request_id == "req-live-input-1").one()
            assert turn.session_input_id == row.id
            archive_input_id = int(row.id)

        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).one()
            assert outbox.kind == SESSION_INPUT_RECEIPT_KIND
            assert outbox.drained_at is not None
            assert outbox.last_error is None
            receipt = live_db.query(LiveSessionInputReceipt).filter(LiveSessionInputReceipt.id == receipt_id).one()
            assert receipt.archive_session_input_id == archive_input_id
            assert receipt.delivery_request_id == "req-live-input-1"

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionInput).filter(SessionInput.session_id == session_id).count() == 1
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_batches_live_mark_drained_commit(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    heartbeat = {
        "device_id": "live-drain-batch",
        "received_at": now,
        "version": "0.5.0",
        "spool_pending": 1,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 1,
        "ship_successes_1h": 1,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "disk_free_bytes": 1,
        "is_offline": 0,
        "raw_json": "{}",
    }

    try:
        with LiveSession() as live_db:
            for index in range(5):
                row = {
                    **heartbeat,
                    "received_at": now + timedelta(milliseconds=index),
                    "sessions_sequence": index,
                }
                assert enqueue_heartbeat_stamp_outbox(live_db, row) is True
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            live_commit_count = 0
            real_live_commit = live_db.commit

            def counted_live_commit():
                nonlocal live_commit_count
                live_commit_count += 1
                return real_live_commit()

            monkeypatch.setattr(live_db, "commit", counted_live_commit)
            result = drain_live_archive_outbox(live_db, archive_db, limit=10, now=now + timedelta(seconds=1))

        assert result.processed == 5
        assert result.drained == 5
        assert result.failed == 0
        assert live_commit_count == 1

        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain-batch").count() == 5
        with LiveSession() as live_db:
            rows = live_db.query(LiveArchiveOutbox).all()
            assert len(rows) == 5
            assert all(row.drained_at is not None for row in rows)
            assert all(row.attempts == 1 for row in rows)
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_retries_after_live_mark_drained_commit_failure(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    heartbeat = {
        "device_id": "live-drain-retry",
        "received_at": now,
        "version": "0.5.0",
        "spool_pending": 1,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 1,
        "ship_successes_1h": 1,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "disk_free_bytes": 1,
        "is_offline": 0,
        "raw_json": "{}",
    }

    try:
        with LiveSession() as live_db:
            enqueue_heartbeat_stamp_outbox(live_db, heartbeat)
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            real_commit = live_db.commit

            def fail_mark_drained_commit_once():
                raise RuntimeError("live commit failed")

            monkeypatch.setattr(live_db, "commit", fail_mark_drained_commit_once)
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)
            monkeypatch.setattr(live_db, "commit", real_commit)

        assert result.processed == 1
        assert result.drained == 0
        assert result.failed == 1
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain-retry").count() == 1
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is None

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 1
        assert result.drained == 1
        assert result.failed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-drain-retry").count() == 1
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is not None
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_failure_stays_retryable(tmp_path):
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with LiveSession() as live_db:
            live_db.add(
                LiveArchiveOutbox(
                    idempotency_key="unsupported:1",
                    kind="unsupported.v1",
                    payload_json="{}",
                )
            )
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert result.processed == 1
        assert result.drained == 0
        assert result.failed == 1
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is None
            assert row.attempts == 1
            assert "Unsupported live archive outbox kind" in (row.last_error or "")
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_runtime_state_is_served_from_the_live_catalog(live: LiveCatalog):
    """The runtime batch is the only writer of phase/tool truth, and catalogd holds it.

    A Runtime Host binds every catalog route's db dependency to nothing, so the
    read below passes the ``None`` production passes: whatever the served
    runtime state is, it did not come from an archive session.
    """

    owner = live.create_user("runtime@live-store-split.test")
    token = live.create_device_token(owner_id=owner, device_id="cinder")
    seeded = live.commit_session(owner_id=owner, device_id="cinder", project="live-runtime")
    now = datetime.now(timezone.utc)

    with live.http_client() as client:
        response = client.post(
            "/agents/runtime/events/batch",
            json={
                "events": [
                    _runtime_event(
                        session_id=seeded.session_id,
                        kind="phase_signal",
                        source="codex_bridge",
                        occurred_at=now,
                        dedupe_key="live-runtime-1",
                        phase="running",
                        tool_name="Shell",
                        freshness_ms=60_000,
                    )
                ]
            },
            headers={"X-Agents-Token": token},
        )

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1

    runtime_state_map = load_runtime_state_map(None, [seeded.session_id])
    runtime_state = runtime_state_map[str(seeded.session_id)]
    assert isinstance(runtime_state, LiveRuntimeState)
    assert runtime_state.phase == "running"
    assert runtime_state.active_tool == "Shell"
    assert runtime_state.phase_source == "codex_bridge"


def test_live_pause_resolution_watermark_rejects_late_request_replay(tmp_path):
    now = datetime.now(timezone.utc)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    runtime_key = runtime_key_for_session("codex", str(session_id))

    def event(kind: str, occurred_at: datetime, dedupe_key: str) -> RuntimeEventIngest:
        return RuntimeEventIngest(
            runtime_key=runtime_key,
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="codex_bridge",
            kind=kind,
            occurred_at=occurred_at,
            freshness_ms=60_000,
            dedupe_key=dedupe_key,
            payload={"request_key": "question-1", "kind": "structured_question", "can_respond": True},
        )

    try:
        with LiveSession() as live_db:
            request = event("pause_request", now, "pause-request-1")
            resolution = event("pause_resolution", now + timedelta(seconds=2), "pause-resolution-1")
            late_replay = event("pause_request", now + timedelta(seconds=1), "pause-request-replay")
            assert ingest_live_runtime_events(live_db, [request]).accepted == 1
            assert ingest_live_runtime_events(live_db, [resolution]).accepted == 1
            assert ingest_live_runtime_events(live_db, [late_replay]).accepted == 1
            live_db.commit()

            state = live_db.query(LiveRuntimeState).filter(LiveRuntimeState.session_id == session_id).one()
            assert state.pending_interaction_id is None
            assert normalize_utc(state.pending_interaction_updated_at) == now + timedelta(seconds=2)
            assert normalize_utc(state.updated_at) >= now + timedelta(seconds=2)
    finally:
        live_engine.dispose()


def test_live_runtime_events_materialize_hot_transcript_preview(live: LiveCatalog):
    """A bridge transcript delta becomes the served provisional preview.

    This used to also seed a stale archive ``SessionLivePreview`` and assert the
    hot row beat it. Under a live catalog ``load_active_provisional_preview_map``
    never opens an archive session at all, so that half pinned a merge
    production cannot perform; what remains is the claim that matters -- the
    delta the bridge just shipped is what the preview read returns.
    """

    owner = live.create_user("preview@live-store-split.test")
    token = live.create_device_token(owner_id=owner, device_id="cinder")
    seeded = live.commit_session(owner_id=owner, device_id="cinder", project="live-preview")
    now = datetime.now(timezone.utc)

    with live.http_client() as client:
        response = client.post(
            "/agents/runtime/events/batch",
            json={
                "events": [
                    _runtime_event(
                        session_id=seeded.session_id,
                        kind="progress_signal",
                        source="codex_bridge_live",
                        occurred_at=now,
                        dedupe_key="live-preview-1",
                        payload={
                            "progress_kind": "bridge_live_transcript_delta",
                            "thread_id": "thread-1",
                            "turn_id": "turn-1",
                            "seq": 2,
                            "live_text": "hot live preview",
                        },
                    )
                ]
            },
            headers={"X-Agents-Token": token},
        )

    assert response.status_code == 200, response.text

    preview = load_active_provisional_preview_map(None, [seeded.session_id])[str(seeded.session_id)]
    assert preview.text == "hot live preview"
    assert preview.event_id == 2
    assert preview.provisional_complete is False
    assert preview.provisional_cursor == f"codex_bridge_live:{seeded.session_id}:thread-1:turn-1:2"


def test_live_archive_outbox_drains_runtime_event_to_archive(tmp_path):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="runtime-outbox",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
                user_state="snoozed",
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

        event = RuntimeEventIngest(
            runtime_key=runtime_key_for_session("codex", str(session_id)),
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="codex_bridge",
            kind="phase_signal",
            phase="running",
            tool_name="Shell",
            occurred_at=now,
            freshness_ms=60_000,
            dedupe_key="runtime-outbox-1",
            payload={},
        )
        with LiveSession() as live_db:
            result = ingest_live_runtime_events(live_db, [event])
            assert enqueue_runtime_events_outbox(live_db, [event]) == 1
            assert enqueue_runtime_events_outbox(live_db, [event]) == 0
            live_db.commit()

        assert result.accepted == 1
        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionRuntimeState).count() == 0
            assert archive_db.query(SessionObservation).count() == 0

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.kind == RUNTIME_EVENT_KIND
            assert row.drained_at is None
            assert "runtime-outbox-1" in row.idempotency_key

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 1
        assert drain_result.drained == 1
        assert drain_result.failed == 0

        with ArchiveSession() as archive_db:
            state = archive_db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == event.runtime_key).one()
            assert state.phase == "running"
            assert state.active_tool == "Shell"
            session = archive_db.query(AgentSession).filter(AgentSession.id == session_id).one()
            assert session.user_state == "active"
            assert session.user_state_at is not None
            assert archive_db.query(SessionObservation).filter(SessionObservation.runtime_key == event.runtime_key).count() == 1

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is not None
            assert row.last_error is None
            assert row.attempts == 1

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionObservation).filter(SessionObservation.runtime_key == event.runtime_key).count() == 1
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_archive_outbox_runtime_event_retry_is_idempotent(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with ArchiveSession() as archive_db:
            session = AgentSession(
                provider="codex",
                environment="test",
                project="runtime-outbox-retry",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
            )
            archive_db.add(session)
            archive_db.commit()
            session_id = session.id

        event = RuntimeEventIngest(
            runtime_key=runtime_key_for_session("codex", str(session_id)),
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="codex_bridge",
            kind="phase_signal",
            phase="running",
            tool_name="Shell",
            occurred_at=now,
            freshness_ms=60_000,
            dedupe_key="runtime-outbox-retry-1",
            payload={},
        )
        with LiveSession() as live_db:
            ingest_live_runtime_events(live_db, [event])
            enqueue_runtime_events_outbox(live_db, [event])
            live_db.commit()

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            real_commit = archive_db.commit

            def fail_archive_commit_once():
                raise RuntimeError("archive commit failed")

            monkeypatch.setattr(archive_db, "commit", fail_archive_commit_once)
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)
            monkeypatch.setattr(archive_db, "commit", real_commit)

        assert drain_result.processed == 1
        assert drain_result.drained == 0
        assert drain_result.failed == 1
        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == event.runtime_key).count() == 0
            assert archive_db.query(SessionObservation).filter(SessionObservation.runtime_key == event.runtime_key).count() == 0
        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            assert row.drained_at is None
            assert row.attempts == 1
            assert "archive commit failed" in (row.last_error or "")

        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 1
        assert drain_result.drained == 1
        assert drain_result.failed == 0

        with LiveSession() as live_db:
            row = live_db.query(LiveArchiveOutbox).one()
            row.drained_at = None
            live_db.commit()
        with LiveSession() as live_db, ArchiveSession() as archive_db:
            drain_result = drain_live_archive_outbox(live_db, archive_db, limit=10)

        assert drain_result.processed == 1
        assert drain_result.drained == 1
        assert drain_result.failed == 0
        with ArchiveSession() as archive_db:
            assert archive_db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == event.runtime_key).count() == 1
            assert archive_db.query(SessionObservation).filter(SessionObservation.runtime_key == event.runtime_key).count() == 1
    finally:
        archive_engine.dispose()
        live_engine.dispose()


def test_live_launch_readiness_projects_and_reaps(tmp_path):
    now = datetime.now(timezone.utc)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    expired_session_id = uuid4()

    try:
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="live_control",
                state="pending",
                command_id=f"launch-{session_id}",
                client_request_id="launch-live-1",
                machine_id="cinder",
                project="repo",
                expires_at=now + timedelta(minutes=2),
                now=now,
            )
            upsert_live_launch_readiness(
                live_db,
                session_id=expired_session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="live_control",
                state="pending",
                command_id=f"launch-{expired_session_id}",
                client_request_id="launch-live-expired",
                machine_id="cinder",
                project="repo",
                expires_at=now - timedelta(seconds=1),
                now=now - timedelta(minutes=5),
            )
            live_db.commit()

        with LiveSession() as live_db:
            readiness = get_live_launch_readiness_by_client_request(
                live_db,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                client_request_id="launch-live-1",
            )
            assert readiness is not None
            assert readiness.session_id == session_id
            assert readiness.launch_state == "launching"

            assert update_live_launch_readiness_state(
                live_db,
                session_id=session_id,
                state="adopted",
                clear_expires=True,
                now=now + timedelta(seconds=1),
            )
            removed = reap_expired_live_launch_readiness(live_db, now=now, limit=10)
            live_db.commit()

        assert removed == 1
        with LiveSession() as live_db:
            row = live_db.get(LiveLaunchReadiness, str(session_id))
            assert row is not None
            assert row.state == "adopted"
            assert row.expires_at is None
            assert live_db.get(LiveLaunchReadiness, str(expired_session_id)) is None
    finally:
        live_engine.dispose()


def test_live_launch_readiness_reap_keeps_pending_managed_launch_outbox(tmp_path):
    now = datetime.now(timezone.utc)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-managed-launch-reap.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    ordinary_expired_session_id = uuid4()
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=42,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="codex",
            project="demo",
            machine_name="cinder",
        ),
        session_id=session_id,
    )

    try:
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=42,
                device_id="cinder",
                provider="codex",
                execution_lifetime="live_control",
                state="pending",
                command_id=f"managed-local-{session_id}",
                client_request_id=None,
                machine_id="cinder",
                project="demo",
                expires_at=now - timedelta(seconds=1),
                now=now - timedelta(minutes=1),
            )
            enqueue_managed_local_launch_outbox(
                live_db,
                plan=plan,
                owner_id=42,
                git_repo=None,
                git_branch=None,
                started_at=now - timedelta(minutes=1),
            )
            upsert_live_launch_readiness(
                live_db,
                session_id=ordinary_expired_session_id,
                owner_id=42,
                device_id="cinder",
                provider="codex",
                execution_lifetime="live_control",
                state="pending",
                command_id=f"launch-{ordinary_expired_session_id}",
                client_request_id=None,
                machine_id="cinder",
                project="demo",
                expires_at=now - timedelta(seconds=1),
                now=now - timedelta(minutes=1),
            )
            removed = reap_expired_live_launch_readiness(live_db, now=now, limit=10)
            live_db.commit()

        assert removed == 1
        with LiveSession() as live_db:
            assert live_db.get(LiveLaunchReadiness, str(session_id)) is not None
            assert live_db.get(LiveLaunchReadiness, str(ordinary_expired_session_id)) is None
    finally:
        live_engine.dispose()


def test_live_launch_readiness_session_map_ignores_expired_rows(tmp_path):
    now = datetime.now(timezone.utc)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    expired_session_id = uuid4()

    try:
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="one_shot",
                state="pending",
                command_id=f"launch-{session_id}",
                client_request_id="launch-session-map",
                machine_id="cinder",
                project="repo",
                expires_at=now + timedelta(minutes=2),
                now=now,
            )
            upsert_live_launch_readiness(
                live_db,
                session_id=expired_session_id,
                owner_id=77,
                device_id="cinder",
                provider="codex",
                execution_lifetime="one_shot",
                state="pending",
                command_id=f"launch-{expired_session_id}",
                client_request_id="launch-session-map-expired",
                machine_id="cinder",
                project="repo",
                expires_at=now - timedelta(seconds=1),
                now=now - timedelta(minutes=5),
            )
            live_db.commit()

        with LiveSession() as live_db:
            readiness = get_live_launch_readiness_by_session_id(live_db, session_id=session_id, now=now)
            assert readiness is not None
            assert readiness.session_id == session_id
            assert readiness.execution_lifetime == "one_shot"
            assert readiness.launch_state == "launching"

            assert get_live_launch_readiness_by_session_id(live_db, session_id=expired_session_id, now=now) is None
            readiness_map = latest_live_launch_readiness_map(live_db, [session_id, expired_session_id], now=now)

        assert set(readiness_map) == {session_id}
    finally:
        live_engine.dispose()


def test_live_launch_readiness_is_served_from_the_catalog_until_it_expires(live: LiveCatalog):
    """Hot readiness is what paints a managed launch before any transcript exists.

    The archive half of this test -- a stale ``SessionLaunchAttempt`` taking
    over once the hot row expired -- ran ``build_session_response`` over an
    archive store, which a Runtime Host never reaches under a live catalog. The
    production claim is narrower and real: the launch RPC publishes readiness
    catalogd serves as ``launching``, and an expired row stops being served
    rather than lingering as a stuck first paint.
    """

    owner = live.create_user("launch@live-store-split.test")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=owner,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="codex",
            project="repo",
            machine_name="cinder",
        )
    )

    created = live.rpc(
        "session.launch.local.create.v2",
        {"launch": _local_launch_rpc(plan, owner_id=owner, started_at=now, expires_at=now + timedelta(minutes=2))},
    )
    assert created["created"] is True

    readiness = latest_live_launch_readiness([plan.session_id], now=now)[plan.session_id]
    assert readiness.launch_state == "launching"
    assert readiness.execution_lifetime == "live_control"
    assert readiness.launch_error_code is None
    assert readiness.launch_error_message is None
    assert readiness.provider == "codex"
    assert readiness.machine_id == "cinder"
    assert readiness.project == "repo"

    assert latest_live_launch_readiness([plan.session_id], now=now + timedelta(minutes=5)) == {}


def test_live_control_lease_feeds_managed_control_overlay(live: LiveCatalog, tmp_path):
    """A managed lease shipped in a heartbeat is what makes control state online.

    ``load_managed_control_state_map`` still takes an archive session for its
    connection lane, but under a live catalog nothing supplies one: its only
    caller, ``build_session_workspace``, is switched off by
    ``get_legacy_workspace_session_factory``. An empty archive session is the
    faithful stand-in, and it makes the point -- the overlay below can only
    have come from catalogd's lease facts.
    """

    owner = live.create_user("control@live-store-split.test")
    token = live.create_device_token(owner_id=owner, device_id="cinder")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=owner,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="codex",
            project="live-control",
            machine_name="cinder",
        )
    )
    live.rpc(
        "session.launch.local.create.v2",
        {"launch": _local_launch_rpc(plan, owner_id=owner, started_at=now, expires_at=now + timedelta(minutes=2))},
    )

    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    try:
        with live.http_client() as client:
            response = client.post(
                "/agents/heartbeat",
                json=_machine_heartbeat(managed_sessions=[_managed_lease(session_id=plan.session_id, sequence=42)]),
                headers={"X-Agents-Token": token},
            )
        assert response.status_code == 204, response.text

        with ArchiveSession() as archive_db:
            overlay = load_managed_control_state_map(archive_db, [plan.session_id])[plan.session_id]

        assert overlay.control_state == "online"
        assert overlay.lease_state == "attached"
        assert overlay.device_id == "cinder"
        assert overlay.machine_id == "cinder"
        assert overlay.sequence == 42
        assert overlay.source == "machine_heartbeat"
    finally:
        archive_engine.dispose()


@pytest.mark.parametrize(
    ("initial_state", "expected_state"),
    [
        ("pending", "adopted"),
        ("dispatched", "adopted"),
        ("failed", "failed"),
        ("abandoned", "abandoned"),
    ],
)
def test_live_control_lease_only_adopts_nonterminal_launch_readiness(tmp_path, initial_state, expected_state):
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    try:
        with LiveSession() as live_db:
            create_live_launch_catalog_shell(
                live_db,
                session_id=session_id,
                thread_id=uuid4(),
                run_id=None,
                owner_id=1,
                provider="cursor",
                device_id="cinder",
                device_name="cinder",
                cwd="/tmp/cursor",
                project="cursor",
                git_repo=None,
                git_branch=None,
                display_name="Cursor",
                initial_prompt=None,
                execution_lifetime="live_control",
                client_request_id=None,
                command_id=f"managed-local-{session_id}",
                launch_fingerprint=None,
                started_at=now,
                expires_at=now + timedelta(minutes=5),
                launch_actor="user",
                launch_surface="cli",
            )
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=1,
                device_id="cinder",
                provider="cursor",
                execution_lifetime="live_control",
                state=initial_state,
                command_id=f"managed-local-{session_id}",
                client_request_id=None,
                machine_id="cinder",
                project="cursor",
                expires_at=now + timedelta(minutes=5),
                now=now,
            )
            readiness = live_db.get(LiveLaunchReadiness, str(session_id))
            assert readiness is not None
            assert readiness.state == initial_state
            readiness.error_code = "launch_failed" if initial_state in {"failed", "abandoned"} else None
            readiness.error_message = "terminal launch failure" if readiness.error_code else None

            lease = SimpleNamespace(
                session_id=session_id,
                provider="cursor",
                machine_id="cinder",
                state="attached",
                sequence=42,
                bridge_status="ready",
                thread_subscription_status=None,
                observed_at=now,
                lease_ttl_ms=60_000,
            )
            upsert_live_control_leases(live_db, [lease], device_id="cinder", received_at=now)
            live_db.flush()

            assert readiness.state == expected_state
            if expected_state == "adopted":
                assert readiness.expires_at is None
                assert readiness.error_code is None
                assert readiness.error_message is None
            else:
                assert readiness.expires_at is not None
                assert readiness.error_code == "launch_failed"
                assert readiness.error_message == "terminal launch failure"
    finally:
        live_engine.dispose()


def test_live_control_lease_missing_from_a_later_snapshot_projects_offline(live: LiveCatalog, tmp_path):
    """An authoritative snapshot that omits a lease detaches it at the new snapshot's clock.

    The archive half -- a stale attached ``SessionConnection`` the fresher live
    row had to beat -- pinned the connection/lease merge inside
    ``load_managed_control_state_map``, and no archive session reaches that
    merge under a live catalog. What production still owes the user survives
    intact: the second heartbeat enumerates no managed sessions, so control
    goes offline stamped at that heartbeat rather than at the stale lease.
    """

    owner = live.create_user("control-missing@live-store-split.test")
    token = live.create_device_token(owner_id=owner, device_id="cinder")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=owner,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="codex",
            project="live-control-missing",
            machine_name="cinder",
        )
    )
    live.rpc(
        "session.launch.local.create.v2",
        {"launch": _local_launch_rpc(plan, owner_id=owner, started_at=now, expires_at=now + timedelta(minutes=2))},
    )

    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    try:
        with live.http_client() as client:
            attached = client.post(
                "/agents/heartbeat",
                json=_machine_heartbeat(managed_sessions=[_managed_lease(session_id=plan.session_id, sequence=1)]),
                headers={"X-Agents-Token": token},
            )
            assert attached.status_code == 204, attached.text
            with ArchiveSession() as archive_db:
                attached_overlay = load_managed_control_state_map(archive_db, [plan.session_id])[plan.session_id]
            assert attached_overlay.control_state == "online"

            # The Machine Agent enumerated its complete managed scope and this
            # session was not in it.
            detached = client.post(
                "/agents/heartbeat",
                json=_machine_heartbeat(managed_sessions=[]),
                headers={"X-Agents-Token": token},
            )
            assert detached.status_code == 204, detached.text

        with ArchiveSession() as archive_db:
            overlay = load_managed_control_state_map(archive_db, [plan.session_id])[plan.session_id]

        assert overlay.control_state == "offline"
        assert overlay.lease_state == "missing"
        assert overlay.reason == "missing_from_snapshot"
        assert overlay.last_control_seen_at > attached_overlay.last_control_seen_at
    finally:
        archive_engine.dispose()


def test_live_terminal_runtime_state_closes_session_for_input(live: LiveCatalog):
    """A terminal runtime signal is what closes the current run to new input.

    ``session_input_block_reason`` reads runtime truth through the catalog, so
    the ``None`` handed to it is the db a Runtime Host binds: the answer is the
    terminal signal the bridge just shipped, not archive bookkeeping.
    """

    owner = live.create_user("terminal@live-store-split.test")
    token = live.create_device_token(owner_id=owner, device_id="cinder")
    seeded = live.commit_session(owner_id=owner, device_id="cinder", project="live-terminal")
    now = datetime.now(timezone.utc)

    assert session_input_block_reason(None, seeded.session_id) is None
    assert session_is_closed_for_input(None, seeded.session_id) is False

    with live.http_client() as client:
        response = client.post(
            "/agents/runtime/events/batch",
            json={
                "events": [
                    _runtime_event(
                        session_id=seeded.session_id,
                        kind="terminal_signal",
                        source="codex_bridge",
                        occurred_at=now,
                        dedupe_key="live-terminal-1",
                        freshness_ms=0,
                        payload={
                            "terminal_state": "process_gone",
                            "terminal_reason": "process_gone",
                            "terminal_source": "codex_bridge",
                        },
                    )
                ]
            },
            headers={"X-Agents-Token": token},
        )

    assert response.status_code == 200, response.text
    assert session_input_block_reason(None, seeded.session_id) == "run_ended"
    assert session_is_closed_for_input(None, seeded.session_id) is True


@pytest.mark.asyncio
async def test_heartbeat_live_stamp_returns_while_archive_bookkeeping_waits(tmp_path, monkeypatch):
    import zerg.routers.heartbeat as heartbeat_router

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-for-live-store-split")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret-for-live-store-split")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setattr(heartbeat_router, "live_store_configured", lambda: True)

    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()
    old_stamp_at = datetime.now(timezone.utc) - timedelta(days=31)
    with LiveSession() as live_db:
        live_db.add(
            LiveHeartbeatStamp(
                device_id="live-split",
                received_at=old_stamp_at,
                version="old",
            )
        )
        live_db.commit()

    live_stamp_done = asyncio.Event()
    archive_bookkeeping_started = asyncio.Event()
    release_archive_bookkeeping = asyncio.Event()
    observations: dict[str, int] = {}

    class LiveSerializer:
        is_configured = True

        async def execute(self, fn, **kwargs):
            assert kwargs["label"] == "heartbeat-stamp"
            observations["archive_pool_checked_out_at_live_write"] = archive_engine.pool.checkedout()
            with LiveSession() as live_db:
                result = fn(live_db)
                live_db.commit()
            live_stamp_done.set()
            return result

    class ArchiveSerializer:
        is_configured = True

        async def execute(self, fn, **kwargs):
            assert kwargs["label"] == "heartbeat-bookkeeping"
            archive_bookkeeping_started.set()
            await release_archive_bookkeeping.wait()
            return {}

        async def execute_after_closing_request_session(self, *_args, **_kwargs):  # pragma: no cover - guard
            raise AssertionError("live-configured heartbeat stamp must not use archive serializer")

    class _FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self, body: bytes) -> None:
            self._body = body

        async def body(self) -> bytes:
            return self._body

    monkeypatch.setattr(heartbeat_router, "get_live_write_serializer", lambda: LiveSerializer())
    monkeypatch.setattr(heartbeat_router, "get_write_serializer", lambda: ArchiveSerializer())

    payload = heartbeat_router.HeartbeatIn(
        version="0.5.0",
        daemon_pid=12345,
        spool_pending_count=2,
        parse_error_count_1h=0,
        consecutive_ship_failures=0,
        disk_free_bytes=50_000_000_000,
        is_offline=False,
        sessions_digest="digest-1",
        sessions_sequence=7,
        managed_sessions=[
            heartbeat_router.ManagedSessionLeaseIn(
                session_id=session_id,
                provider="codex",
                machine_id="live-split",
                state="attached",
                phase="idle",
                bridge_status="ready",
                thread_subscription_status="active",
                lease_ttl_ms=60_000,
                sequence=7,
            )
        ],
    )

    request_db = ArchiveSession()
    request_db.execute(text("SELECT 1"))
    try:
        response = await asyncio.wait_for(
            heartbeat_router.ingest_heartbeat(
                payload,
                _FakeRequest(payload.model_dump_json().encode()),
                request_db,
                SimpleNamespace(device_id="live-split", id="token-1"),
            ),
            timeout=0.5,
        )
        assert response.status_code == 204
        assert live_stamp_done.is_set()
        await asyncio.wait_for(archive_bookkeeping_started.wait(), timeout=0.5)
        assert not release_archive_bookkeeping.is_set()

        with LiveSession() as live_db:
            row = live_db.query(LiveHeartbeatStamp).filter(LiveHeartbeatStamp.device_id == "live-split").one()
            assert row.spool_pending == 2
            assert row.sessions_digest == "digest-1"
            assert row.sessions_sequence == 7
            assert row.version == "0.5.0"
            control = live_db.query(LiveControlLease).filter(LiveControlLease.session_id == str(session_id)).one()
            assert control.device_id == "live-split"
            assert control.provider == "codex"
            assert control.state == "attached"
            assert control.sequence == 7
            live_session = live_db.get(LiveSessionRow, str(session_id))
            assert live_session is not None
            assert live_session.device_id == "live-split"
            assert live_session.provider == "codex"
            assert live_session.state == "attached"
            assert live_session.last_seen_at is not None
            assert live_db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.kind == HEARTBEAT_STAMP_KIND).count() == 0

        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "live-split").count() == 0
    finally:
        release_archive_bookkeeping.set()
        await asyncio.sleep(0)
        archive_engine.dispose()
        live_engine.dispose()

    assert observations == {"archive_pool_checked_out_at_live_write": 0}


@pytest.mark.asyncio
async def test_heartbeat_live_store_requires_configured_live_serializer(tmp_path, monkeypatch):
    import zerg.routers.heartbeat as heartbeat_router

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-for-live-store-split")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret-for-live-store-split")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setattr(heartbeat_router, "live_store_configured", lambda: True)

    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    class UnconfiguredLiveSerializer:
        is_configured = False

    class ArchiveSerializer:
        is_configured = True

    class _FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self, body: bytes) -> None:
            self._body = body

        async def body(self) -> bytes:
            return self._body

    monkeypatch.setattr(heartbeat_router, "get_live_write_serializer", lambda: UnconfiguredLiveSerializer())
    monkeypatch.setattr(heartbeat_router, "get_write_serializer", lambda: ArchiveSerializer())

    payload = heartbeat_router.HeartbeatIn(
        version="0.5.0",
        daemon_pid=12345,
        spool_pending_count=0,
        parse_error_count_1h=0,
        consecutive_ship_failures=0,
        disk_free_bytes=1,
        is_offline=False,
    )

    request_db = ArchiveSession()
    try:
        with pytest.raises(heartbeat_router.HTTPException) as exc:
            await heartbeat_router.ingest_heartbeat(
                payload,
                _FakeRequest(payload.model_dump_json().encode()),
                request_db,
                SimpleNamespace(device_id="live-unconfigured", id="token-1"),
            )
    finally:
        archive_engine.dispose()

    assert exc.value.status_code == 503
    assert "Live Store write serializer is not configured" in str(exc.value.detail)


def test_runtime_events_touch_live_session_candidates(tmp_path):
    """Runtime signals must feed the active-session candidate index.

    Unmanaged/Shadow sessions never hold a managed lease; without this,
    configuring the Live Store silently drops them from the active list.
    """
    now = datetime.now(timezone.utc)
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    session_id = uuid4()

    event = RuntimeEventIngest(
        runtime_key=runtime_key_for_session("claude", str(session_id)),
        session_id=session_id,
        provider="claude",
        device_id="cinder",
        source="e2e",
        kind="phase_signal",
        phase="running",
        tool_name="bash",
        occurred_at=now,
        freshness_ms=600_000,
        dedupe_key="touch-live-1",
        payload={},
    )

    try:
        with LiveSession() as live_db:
            live_db.add(
                LiveSessionCatalog(
                    session_id=str(session_id),
                    provider="claude",
                    environment="production",
                    started_at=now,
                )
            )
            ingest_live_runtime_events(live_db, [event])
            live_db.commit()

        with LiveSession() as live_db:
            row = live_db.get(LiveSessionRow, str(session_id))
            assert row is not None
            assert row.state == "observed"
            assert row.provider == "claude"
            assert row.device_id == "cinder"

            active_ids = list_active_live_session_ids(live_db, limit=10, days_back=7, now=now)
            assert session_id in active_ids

        # Terminal signals keep the session in the candidate set — a
        # completed run is not a gone session.
        terminal = RuntimeEventIngest(
            runtime_key=runtime_key_for_session("claude", str(session_id)),
            session_id=session_id,
            provider="claude",
            device_id="cinder",
            source="e2e",
            kind="terminal_signal",
            phase="completed",
            occurred_at=now + timedelta(seconds=5),
            dedupe_key="touch-live-2",
            payload={},
        )
        with LiveSession() as live_db:
            ingest_live_runtime_events(live_db, [terminal])
            live_db.commit()

        with LiveSession() as live_db:
            active_ids = list_active_live_session_ids(live_db, limit=10, days_back=7, now=now)
            assert session_id in active_ids
    finally:
        live_engine.dispose()


def test_served_runtime_state_comes_from_the_catalog_not_the_archive_row(live: LiveCatalog, tmp_path):
    """``session_runtime_state`` is legacy evidence, not served authority.

    This used to pin the cross-lane merge: an archive progress-derived idle row
    stamped at write time had to lose to a fresher live phase_signal on signal
    clock. Under a live catalog there is no merge to get wrong --
    ``load_runtime_state_map`` returns catalogd's runtime fact and never reads
    the session it was handed -- and ``_runtime_state_newer_than`` is reachable
    only from the branch a Runtime Host does not take. The stronger claim
    replaces it: the archive row below is written last, for the same runtime
    key, with a different phase, and still cannot reach the served view.
    """

    owner = live.create_user("merge@live-store-split.test")
    token = live.create_device_token(owner_id=owner, device_id="cinder")
    seeded = live.commit_session(owner_id=owner, device_id="cinder", project="live-merge")
    now = datetime.now(timezone.utc)
    runtime_key = runtime_key_for_session("codex", str(seeded.session_id))

    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    try:
        with live.http_client() as client:
            response = client.post(
                "/agents/runtime/events/batch",
                json={
                    "events": [
                        _runtime_event(
                            session_id=seeded.session_id,
                            kind="phase_signal",
                            source="codex_bridge",
                            occurred_at=now - timedelta(seconds=5),
                            dedupe_key="merge-running-1",
                            phase="running",
                            tool_name="bash",
                            freshness_ms=600_000,
                        )
                    ]
                },
                headers={"X-Agents-Token": token},
            )
        assert response.status_code == 200, response.text

        # Transcript ingest, writing its progress-derived phase into the cold
        # lane after the live one landed.
        with ArchiveSession() as archive_db:
            ingest_runtime_events(
                archive_db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key,
                        session_id=seeded.session_id,
                        provider="codex",
                        device_id="cinder",
                        source="agents_ingest",
                        kind="progress_signal",
                        occurred_at=now - timedelta(seconds=60),
                        dedupe_key="merge-progress-1",
                        payload={"progress_kind": "transcript_append"},
                    )
                ],
            )
            archive_db.commit()

        with ArchiveSession() as archive_db:
            archive_row = archive_db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
            assert archive_row.phase != "running"

            served = load_runtime_state_map(archive_db, [seeded.session_id])[str(seeded.session_id)]

        assert isinstance(served, LiveRuntimeState)
        assert served.phase == "running"
        assert served.active_tool == "bash"
    finally:
        archive_engine.dispose()
