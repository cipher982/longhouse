from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid4
from uuid import uuid5

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.session_pause_requests import make_pause_request_key


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-interactions-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_catalogd_owns_permission_registration_resolution_and_poll(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    runtime_key = f"claude:{session_id}"
    request_key = make_pause_request_key(
        provider="claude",
        runtime_key=runtime_key,
        provider_request_id="tool-1",
    )
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="claude",
                environment="dev",
                device_id="cinder",
                started_at=now,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    # Functional ownership test, not an RPC latency benchmark. Parallel full
    # suite workers can starve this private daemon beyond the production 1s
    # hard deadline on developer/CI hosts.
    client = CatalogClient(socket_path, default_timeout_seconds=5.0)
    try:
        registration = {
            "interaction": {
                "session_id": session_id,
                "runtime_key": runtime_key,
                "provider": "claude",
                "device_id": "cinder",
                "source": "claude_permission_gate",
                "reply_transport": "claude_pretooluse_pull",
                "provider_request_id": "tool-1",
                "request_key": request_key,
                "kind": "permission_prompt",
                "tool_name": "Bash",
                "title": "Permission: Bash",
                "summary": "Claude wants to use Bash.",
                "request_payload": {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
                "can_respond": True,
                "occurred_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=20)).isoformat(),
                "single_active": True,
            }
        }
        registered = await client.call("interaction.register.v2", registration)
        interaction = registered["interaction"]
        interaction_id = interaction["id"]
        assert interaction["reply_transport"] == "claude_pretooluse_pull"
        pending = await client.call(
            "interaction.decision.read.v2",
            {"session_id": session_id, "interaction_id": interaction_id, "request_key": None},
        )
        assert pending["resolved"] is False
        listed = await client.call(
            "interaction.list.v2",
            {"session_id": session_id, "status": "pending", "limit": 20},
        )
        assert [item["id"] for item in listed["interactions"]] == [interaction_id]

        resolved = await client.call(
            "interaction.resolve.v2",
            {
                "session_id": session_id,
                "interaction_id": interaction_id,
                "status": "resolved",
                "response_payload": {
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "Approved remotely",
                },
                "response_text": "Approved remotely",
                "resolved_at": now.isoformat(),
            },
        )
        assert resolved["resolved"] is True
        decision = await client.call(
            "interaction.decision.read.v2",
            {"session_id": session_id, "interaction_id": interaction_id, "request_key": None},
        )
        assert decision["decision"] == "allow"
        assert decision["reason"] == "Approved remotely"
        replay = await client.call("interaction.register.v2", registration)
        assert replay["interaction"]["status"] == "resolved"
        assert replay["interaction"]["can_respond"] is False

        expired_registration = {"interaction": dict(registration["interaction"])}
        expired_registration["interaction"].update(
            {
                "source": "cursor_permission_gate",
                "provider": "cursor",
                "reply_transport": "cursor_permission_poll",
                "provider_request_id": "cursor-expired",
                "request_key": f"cursor:cursor:{session_id}:cursor-expired",
                "occurred_at": (now - timedelta(seconds=30)).isoformat(),
                "expires_at": (now - timedelta(seconds=10)).isoformat(),
            }
        )
        expired = (await client.call("interaction.register.v2", expired_registration))["interaction"]
        assert expired["reply_transport"] == "cursor_permission_poll"
        deadline_decision = await client.call(
            "interaction.decision.read.v2",
            {"session_id": session_id, "interaction_id": expired["id"], "request_key": None},
        )
        assert deadline_decision["decision"] == "deny"
        pending_after_deadline = await client.call(
            "interaction.list.v2",
            {"session_id": session_id, "status": "pending", "limit": 20},
        )
        assert expired["id"] not in {item["id"] for item in pending_after_deadline["interactions"]}
        late_allow = await client.call(
            "interaction.resolve.v2",
            {
                "session_id": session_id,
                "interaction_id": expired["id"],
                "status": "resolved",
                "response_payload": {"permissionDecision": "allow"},
                "response_text": "too late",
                "resolved_at": now.isoformat(),
            },
        )
        assert late_allow["resolved"] is False
        assert late_allow["interaction"]["status"] == "expired"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        row = db.get(LiveInteractionRequest, interaction_id)
        assert row.status == "resolved"
        runtime = db.query(LiveRuntimeState).filter_by(runtime_key=runtime_key).one()
        assert runtime.pending_interaction_id is None
        assert db.query(LiveArchiveOutbox).count() == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_repairs_only_the_exact_legacy_permission_gate_record(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    runtime_key = f"cursor:{session_id}"
    request_key = make_pause_request_key(
        provider="cursor",
        runtime_key=runtime_key,
        provider_request_id="legacy-shell",
    )
    interaction_id = str(uuid5(NAMESPACE_URL, f"longhouse-pause:{request_key}"))
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=session_id,
                provider="cursor",
                environment="prod",
                device_id="zerg",
                started_at=now,
            )
        )
        db.add(
            LiveRuntimeState(
                runtime_key=runtime_key,
                session_id=UUID(session_id),
                provider="cursor",
                phase="idle",
                phase_source="cursor_hook",
                timeline_anchor_at=now,
                pending_interaction_id=request_key,
                pending_interaction_kind="permission_prompt",
                pending_interaction_opened_at=now,
                pending_interaction_updated_at=now,
                pending_interaction_can_respond=1,
                pending_interaction_projection_json={"id": interaction_id, "request_key": request_key},
                runtime_version=1,
                updated_at=now,
            )
        )
        db.add(
            LiveInteractionRequest(
                id=interaction_id,
                session_id=session_id,
                runtime_key=runtime_key,
                provider="cursor",
                source="claude_permission_gate",
                reply_transport="claude_pretooluse_pull",
                provider_request_id="legacy-shell",
                request_key=request_key,
                kind="permission_prompt",
                request_payload_json={},
                projection_json={
                    "tool_name": "Shell",
                    "title": "Permission: Shell",
                    "summary": "Claude wants to use Shell.",
                },
                status="pending",
                can_respond=1,
                occurred_at=now,
                expires_at=None,
                last_seen_at=now,
                updated_at=now,
            )
        )
        db.commit()
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path, default_timeout_seconds=5.0)
    try:
        repaired = await client.call(
            "interaction.repair.expire.v2",
            {
                "session_id": session_id,
                "interaction_id": interaction_id,
                "expected_updated_at": now.isoformat(),
                "expected_source": "claude_permission_gate",
                "expected_reply_transport": "claude_pretooluse_pull",
                "now": (now + timedelta(seconds=1)).isoformat(),
                "dry_run": True,
            },
        )
        assert repaired["repaired"] is False
        assert repaired["dry_run"] is True
        assert repaired["interaction"]["status"] == "pending"

        repaired = await client.call(
            "interaction.repair.expire.v2",
            {
                "session_id": session_id,
                "interaction_id": interaction_id,
                "expected_updated_at": now.isoformat(),
                "expected_source": "claude_permission_gate",
                "expected_reply_transport": "claude_pretooluse_pull",
                "now": (now + timedelta(seconds=1)).isoformat(),
                "dry_run": False,
            },
        )
        assert repaired["repaired"] is True
        assert repaired["interaction"]["status"] == "expired"
        assert repaired["interaction"]["can_respond"] is False

        replay = await client.call(
            "interaction.repair.expire.v2",
            {
                "session_id": session_id,
                "interaction_id": interaction_id,
                "expected_updated_at": now.isoformat(),
                "expected_source": "claude_permission_gate",
                "expected_reply_transport": "claude_pretooluse_pull",
                "now": (now + timedelta(seconds=2)).isoformat(),
                "dry_run": False,
            },
        )
        assert replay == {"repaired": False, "reason": "compare_and_set_failed", "commit_seq": replay["commit_seq"]}
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        row = db.get(LiveInteractionRequest, interaction_id)
        assert row.status == "expired"
        assert row.expires_at is None
        runtime = db.query(LiveRuntimeState).filter_by(runtime_key=runtime_key).one()
        assert runtime.pending_interaction_id is None
        assert runtime.pending_interaction_can_respond == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_runtime_only_permission_survives_interaction_table_migration(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    runtime_key = f"claude:{session_id}"
    request_key = make_pause_request_key(
        provider="claude",
        runtime_key=runtime_key,
        provider_request_id="legacy-tool",
    )
    interaction_id = str(uuid5(NAMESPACE_URL, f"longhouse-pause:{request_key}"))
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=session_id,
                provider="claude",
                environment="dev",
                device_id="cinder",
                started_at=now,
            )
        )
        db.add(
            LiveRuntimeState(
                runtime_key=runtime_key,
                session_id=UUID(session_id),
                provider="claude",
                phase="blocked",
                phase_source="legacy_permission_gate",
                timeline_anchor_at=now,
                pending_interaction_id=request_key,
                pending_interaction_kind="permission_prompt",
                pending_interaction_opened_at=now,
                pending_interaction_updated_at=now,
                pending_interaction_can_respond=1,
                pending_interaction_projection_json={
                    "id": interaction_id,
                    "request_key": request_key,
                    "session_id": session_id,
                    "runtime_key": runtime_key,
                    "kind": "permission_prompt",
                    "status": "pending",
                    "provider": "claude",
                    "can_respond": True,
                    "questions": [],
                },
                runtime_version=1,
                updated_at=now,
            )
        )
        db.commit()
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        listed = await client.call(
            "interaction.list.v2",
            {"session_id": session_id, "status": "pending", "limit": 20},
        )
        legacy = listed["interactions"][0]
        assert legacy["provider_request_id"] == "legacy-tool"
        assert legacy["source"] == "claude_permission_gate"
        assert legacy["reply_transport"] == "claude_pretooluse_pull"
        await client.call(
            "interaction.resolve.v2",
            {
                "session_id": session_id,
                "interaction_id": interaction_id,
                "status": "resolved",
                "response_payload": {"permissionDecision": "allow"},
                "response_text": "Approved during migration",
                "resolved_at": now.isoformat(),
            },
        )
        decision = await client.call(
            "interaction.decision.read.v2",
            {"session_id": session_id, "interaction_id": interaction_id, "request_key": None},
        )
        assert decision["decision"] == "allow"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_runtime_pause_event_populates_catalog_interaction(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="codex",
                environment="dev",
                device_id="cinder",
                started_at=now,
            )
        )
    engine.dispose()
    event = {
        "runtime_key": f"codex:{session_id}",
        "session_id": session_id,
        "thread_id": None,
        "run_id": None,
        "provider": "codex",
        "device_id": "cinder",
        "source": "codex_bridge",
        "kind": "pause_request",
        "phase": None,
        "tool_name": "AskUserQuestion",
        "occurred_at": now.isoformat(),
        "freshness_ms": None,
        "dedupe_key": "pause-1",
        "payload": {
            "provider_request_id": "ask-1",
            "kind": "structured_question",
            "can_respond": True,
            "provider_ref": {"reply_transport": "managed_push"},
            "request_payload": {"question": "Ship it?", "options": ["yes", "no"]},
        },
    }
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        await client.call("session.runtime.apply.v2", {"events": [event]})
        listed = await client.call(
            "interaction.list.v2",
            {"session_id": session_id, "status": "pending", "limit": 20},
        )
        assert listed["total"] == 1
        assert listed["interactions"][0]["reply_transport"] == "managed_push"
        assert listed["interactions"][0]["projection"]["can_respond"] is True
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_permission_and_pause_routes_use_catalog_without_db(daemon_paths, monkeypatch):
    from zerg.routers import permission_gate
    from zerg.routers import session_chat

    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="claude",
                environment="dev",
                device_id="cinder",
                started_at=now,
            )
        )
    engine.dispose()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    monkeypatch.setattr(permission_gate.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(session_chat.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: client)
    try:
        ack = await permission_gate.register_permission_request(
            permission_gate.PermissionRequestIn(
                session_id=session_id,
                tool_use_id="tool-route-1",
                tool_name="Bash",
                tool_input={"command": "pwd"},
                occurred_at=now,
            ),
            db=None,
            _token=None,
        )
        assert ack.status == "pending"
        response = await session_chat._respond_to_live_pause_request(
            source_session=SimpleNamespace(id=UUID(session_id)),
            owner_id=7,
            pause_request_id=ack.pause_request_id,
            body=session_chat.PauseRequestResponseRequest(decision="answer"),
            db=None,
        )
        assert response.status == "resolved"
        decision = await permission_gate.get_permission_decision(
            session_id=session_id,
            tool_use_id="tool-route-1",
            pause_request_id=ack.pause_request_id,
            provider="claude",
            db=None,
            _token=None,
        )
        assert decision.decision == "allow"
        assert decision.resolved is True
    finally:
        await client.close()
        await daemon.close()
