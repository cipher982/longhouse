from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.protocol import HEADER_BYTES
from zerg.catalogd.protocol import MAX_PAYLOAD_BYTES
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import encode_frame
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
from zerg.services import catalog_read_gateway


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-reads-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_session(connection, *, session_id: str, device_id: str, now: datetime, project: str = "zerg") -> None:
    thread_id = str(uuid4())
    run_id = str(uuid4())
    connection.execute(
        LiveSessionCatalog.__table__.insert().values(
            session_id=session_id,
            provider="codex",
            environment="prod",
            project=project,
            device_id=device_id,
            device_name="Cinder",
            cwd="/Users/david/git/zerg",
            git_repo="https://github.com/cipher982/longhouse.git",
            git_branch="main",
            started_at=now - timedelta(hours=2),
            last_activity_at=now - timedelta(minutes=2),
            primary_thread_id=thread_id,
        )
    )
    connection.execute(
        LiveTimelineCard.__table__.insert().values(
            session_id=session_id,
            provider="codex",
            environment="prod",
            project=project,
            device_id=device_id,
            cwd="/Users/david/git/zerg",
            started_at=now - timedelta(hours=2),
            last_activity_at=now - timedelta(minutes=2),
            user_messages=2,
            assistant_messages=3,
            tool_calls=4,
            parser_revision="parser-v2",
        )
    )
    connection.execute(
        LiveSessionThread.__table__.insert().values(
            id=thread_id,
            session_id=session_id,
            provider="codex",
            is_primary=1,
            created_at=now - timedelta(hours=2),
            updated_at=now,
        )
    )
    connection.execute(
        LiveSessionRun.__table__.insert().values(
            id=run_id,
            thread_id=thread_id,
            provider="codex",
            host_id=device_id,
            launch_origin="longhouse_spawned",
            started_at=now - timedelta(hours=1),
        )
    )
    connection.execute(
        LiveSessionConnection.__table__.insert().values(
            run_id=run_id,
            control_plane="managed_local",
            acquisition_kind="launch_local",
            state="attached",
            device_id=device_id,
            can_send_input=1,
            acquired_at=now - timedelta(hours=1),
            last_health_at=now,
        )
    )
    connection.execute(
        LiveSessionThreadAlias.__table__.insert().values(
            thread_id=thread_id,
            provider="codex",
            alias_kind="provider_session_id",
            alias_value=f"provider-{session_id}",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    connection.execute(
        LiveRuntimeState.__table__.insert().values(
            runtime_key=f"codex:{session_id}",
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            provider="codex",
            device_id=device_id,
            phase="quiescent",
            phase_source="hook",
            timeline_anchor_at=now,
            runtime_version=7,
            updated_at=now,
        )
    )
    connection.execute(
        LiveLaunchReadiness.__table__.insert().values(
            session_id=session_id,
            owner_id="7",
            provider="codex",
            device_id=device_id,
            execution_lifetime="live_control",
            state="adopted",
            created_at=now,
            updated_at=now,
        )
    )


def test_catalog_gateway_normalizes_missing_file_backing(monkeypatch):
    monkeypatch.setattr(
        catalog_read_gateway,
        "catalogd_paths",
        lambda: (_ for _ in ()).throw(RuntimeError("not file backed")),
    )
    with pytest.raises(catalog_read_gateway.CatalogReadError, match="temporarily unavailable"):
        catalog_read_gateway.active_owner_id()


@pytest.mark.asyncio
async def test_active_owner_read_is_catalog_owned(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {"id": 1, "email": "old@example.com", "role": "USER", "is_active": False},
                {"id": 7, "email": "owner@example.com", "role": "ADMIN", "is_active": True},
            ],
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call("auth.owner.get.v2", {})
        assert result["found"] is True
        assert result["owner_id"] == 7
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_session_timeline_and_read_return_assembled_snapshot_facts(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    pending_id = "33333333-3333-4333-8333-333333333333"
    with engine.begin() as connection:
        _seed_session(connection, session_id=first_id, device_id="cinder", now=now)
        _seed_session(connection, session_id=second_id, device_id="clifford", now=now - timedelta(hours=1))
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=pending_id,
                provider="codex",
                environment="prod",
                project="zerg",
                device_id="cinder",
                started_at=now,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call(
            "session.timeline.list.v2",
            {
                "project": "zerg",
                "provider": None,
                "environment": None,
                "include_test": False,
                "hide_autonomous": True,
                "include_automation": False,
                "device_id": None,
                "days_back": 7,
                "limit": 1,
                "offset": 0,
            },
        )
        assert result["commit_seq"] == "0"
        assert result["total"] == 2
        assert result["has_real_sessions"] is True
        assert len(result["rows"]) == 1
        facts = result["rows"][0]["facts"]
        assert facts["catalog"]["session_id"] == first_id
        assert facts["card"]["tool_calls"] == 4
        assert facts["runtime"]["phase"] == "quiescent"
        assert facts["readiness"]["state"] == "adopted"
        assert facts["primary_thread"]["id"] == facts["catalog"]["primary_thread_id"]
        assert facts["latest_run"]["thread_id"] == facts["primary_thread"]["id"]
        assert facts["connections"][0]["can_send_input"] == 1
        assert facts["provider_alias"] is None
        assert "display_phase" not in facts and "status" not in facts

        read = await client.call("session.read.v2", {"session_id": first_id})
        assert read["found"] is True
        assert read["facts"]["catalog"]["session_id"] == first_id
        assert read["facts"]["provider_alias"] == f"provider-{first_id}"
        assert read["observed_at"].endswith("+00:00")
        pending = await client.call("session.read.v2", {"session_id": pending_id})
        assert pending["found"] is True
        assert pending["facts"]["catalog"]["session_id"] == pending_id
        assert pending["facts"]["card"] is None
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_session_read_validation_and_prefix_missing_ambiguous_found(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    first_id = "aaaaaaaa-1111-4111-8111-111111111111"
    second_id = "aaaaaaaa-2222-4222-8222-222222222222"
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {
                    "email": "david010@example.com",
                    "display_name": " David Rose ",
                    "is_active": True,
                },
                {
                    "email": "other@example.com",
                    "display_name": "Other User",
                    "is_active": True,
                },
            ],
        )
        _seed_session(connection, session_id=first_id, device_id="cinder", now=now)
        _seed_session(connection, session_id=second_id, device_id="cinder", now=now)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        missing_prefix = await client.call("session.prefix.resolve.v2", {"prefix": "bbbb"})
        assert missing_prefix["status"] == "missing"
        assert missing_prefix["session"] is None and missing_prefix["owner"] is None
        ambiguous = await client.call("session.prefix.resolve.v2", {"prefix": "aaaaaaaa"})
        assert ambiguous["status"] == "ambiguous" and ambiguous["session_id"] is None
        assert ambiguous["session"] is None and ambiguous["owner"] is None
        found = await client.call("session.prefix.resolve.v2", {"prefix": "aaaaaaaa-1111"})
        assert found["status"] == "unique" and found["session_id"] == first_id
        assert found["session"] == {
            "session_id": first_id,
            "provider": "codex",
            "device_name": "Cinder",
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "ended_at": None,
        }
        assert found["owner"] == {"display_name": "David Rose", "email_local": "david010"}
        assert set(found["session"]) == {"session_id", "provider", "device_name", "started_at", "ended_at"}
        missing = await client.call("session.read.v2", {"session_id": str(uuid4())})
        assert missing["found"] is False and missing["facts"] is None
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.read.v2", {"session_id": "not-a-uuid"})
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_enrollment_excludes_revoked_and_workspaces_are_owner_device_scoped(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert(),
            [
                {
                    "id": str(uuid4()),
                    "owner_id": 7,
                    "device_id": "cinder",
                    "token_hash": "a" * 64,
                    "created_at": now,
                    "revoked_at": None,
                },
                {
                    "id": str(uuid4()),
                    "owner_id": 7,
                    "device_id": "old",
                    "token_hash": "b" * 64,
                    "created_at": now,
                    "revoked_at": now,
                },
                {
                    "id": str(uuid4()),
                    "owner_id": 8,
                    "device_id": "private",
                    "token_hash": "c" * 64,
                    "created_at": now,
                    "revoked_at": None,
                },
            ],
        )
        _seed_session(connection, session_id=str(uuid4()), device_id="cinder", now=now)
        _seed_session(connection, session_id=str(uuid4()), device_id="private", now=now, project="secret")
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        enrollments = await client.call("machine.enrollment.list.v2", {"owner_id": 7})
        assert [row["device_id"] for row in enrollments["enrollments"]] == ["cinder"]
        workspaces = await client.call(
            "machine.workspace.list.v2",
            {"owner_id": 7, "device_id": "cinder", "limit": 12, "days_back": 45},
        )
        assert [row["path"] for row in workspaces["workspaces"]] == ["/Users/david/git/zerg"]
        assert workspaces["workspaces"][0]["label"] == "longhouse (main)"
        assert (
            await client.call(
                "machine.workspace.list.v2",
                {"owner_id": 7, "device_id": "private", "limit": 12, "days_back": 45},
            )
        )["workspaces"] == []
        assert (
            await client.call(
                "machine.workspace.list.v2",
                {"owner_id": 7, "device_id": "old", "limit": 12, "days_back": 45},
            )
        )["workspaces"] == []
    finally:
        await client.close()
        await daemon.close()


def test_maximum_timeline_page_fits_one_protocol_frame(daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    oversized = "🚀" * 40_000
    session_id = "ffffffff-1111-4111-8111-ffffffffffff"
    with engine.begin() as connection:
        _seed_session(connection, session_id=session_id, device_id="cinder", now=now)
        connection.execute(
            LiveSessionCatalog.__table__.update()
            .where(LiveSessionCatalog.session_id == session_id)
            .values(
                cwd=oversized,
                git_repo=oversized,
                summary=oversized,
                first_user_message_preview=oversized,
            )
        )
        connection.execute(
            LiveTimelineCard.__table__.update()
            .where(LiveTimelineCard.session_id == session_id)
            .values(cwd=oversized, first_user_message_preview=oversized)
        )
        connection.execute(
            LiveRuntimeState.__table__.update()
            .where(LiveRuntimeState.runtime_key == f"codex:{session_id}")
            .values(
                pending_interaction_id=oversized,
                pending_interaction_kind="structured_question",
                pending_interaction_projection_json={
                    "id": oversized,
                    "request_key": oversized,
                    "summary": oversized,
                    "questions": [
                        {
                            "id": oversized,
                            "header": oversized,
                            "question": oversized,
                            "options": [
                                {"label": oversized, "description": oversized, "value": oversized} for _ in range(20)
                            ],
                        }
                        for _ in range(20)
                    ],
                },
            )
        )
        connection.execute(
            LiveLaunchReadiness.__table__.update()
            .where(LiveLaunchReadiness.session_id == session_id)
            .values(error_message=oversized)
        )
        thread_id = connection.execute(
            LiveSessionCatalog.__table__.select()
            .with_only_columns(LiveSessionCatalog.primary_thread_id)
            .where(LiveSessionCatalog.session_id == session_id)
        ).scalar_one()
        run_id = connection.execute(
            LiveSessionRun.__table__.select()
            .with_only_columns(LiveSessionRun.id)
            .where(LiveSessionRun.thread_id == thread_id)
        ).scalar_one()
        for connection_index in range(7):
            connection.execute(
                LiveSessionConnection.__table__.insert().values(
                    run_id=run_id,
                    control_plane=f"plane-{connection_index}-{oversized}",
                    acquisition_kind="spawned_control",
                    state="attached",
                    device_id=oversized,
                    can_send_input=1,
                    can_interrupt=1,
                    can_terminate=1,
                    can_tail_output=1,
                    can_resume=1,
                    acquired_at=now,
                    last_health_at=now,
                )
            )

    result = CatalogStore(engine).list_session_timeline(
        project=None,
        provider=None,
        environment=None,
        include_test=False,
        hide_autonomous=False,
        include_automation=True,
        device_id=None,
        days_back=90,
        limit=100,
        offset=0,
    )
    detail = CatalogStore(engine).read_session(session_id=session_id)
    result["rows"] *= 100
    result["total"] = 100
    response = CatalogRpcResponse(id="0" * 32, result=result)
    payload_bytes = len(
        json.dumps(response.to_wire(), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    )
    row_sizes = {
        key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for key, value in result["rows"][0]["facts"].items()
    }
    assert payload_bytes < MAX_PAYLOAD_BYTES, (payload_bytes, row_sizes)
    frame = encode_frame(response)
    engine.dispose()

    assert len(result["rows"]) == 100
    assert len(result["rows"][0]["facts"]["runtime"]["pending_interaction_projection_json"]["questions"]) == 3
    assert len(detail["facts"]["runtime"]["pending_interaction_projection_json"]["questions"]) == 3
    assert len(frame) - HEADER_BYTES < MAX_PAYLOAD_BYTES
