from types import SimpleNamespace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

import zerg.services.session_workspace as session_workspace_module
import zerg.services.storage_v2_workspace as workspace_module
import zerg.routers.session_chat as session_chat_module
from zerg.catalogd.schema import create_catalog_engine, initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveUser
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.live_control_catalog import load_live_control_session_snapshot
from zerg.services.catalog_read_gateway import CatalogReadError


class _Catalog:
    async def call(self, method, params):
        assert method == "storage.session.read.v2"
        return {
            "found": True,
            "commit_seq": "8",
            "session": {"owner_id": "42", "updated_at": "2026-07-12T12:00:00Z"},
        }


def test_live_catalog_workspace_dependency_does_not_open_legacy_database(monkeypatch):
    monkeypatch.setattr(session_workspace_module.database_module, "live_catalog_enabled", lambda: True)

    def forbidden():
        raise AssertionError("legacy database factory must not be constructed")

    monkeypatch.setattr(session_workspace_module, "get_session_factory", forbidden)
    assert session_workspace_module.get_legacy_workspace_session_factory() is None


@pytest.mark.asyncio
async def test_storage_v2_workspace_composes_catalog_shell_and_tail(monkeypatch):
    session_id = uuid4()
    session = SimpleNamespace(
        provider="codex",
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True),
        model_dump=lambda **_kwargs: {"id": str(session_id), "lifecycle": "open", "capabilities": {}},
    )

    async def read_page(**kwargs):
        timing = kwargs.pop("timing")
        assert isinstance(timing, workspace_module.ServerTimingRecorder)
        assert timing.product_surface is None
        assert "catalog_session" in (timing.header_value() or "")
        assert "storage_manifest" in (timing.header_value() or "")
        assert kwargs == {
            "session_id": session_id,
            "owner_id": "42",
            "cursor": None,
            "anchor": "tail",
            "limit": 50,
        }
        return {
            "generation_id": str(uuid4()),
            "events": [
                {
                    "event_id": "event-1",
                    "cursor": "cursor-1",
                    "timestamp": "2026-07-12T12:00:00+00:00",
                    "role": "user",
                    "content_text": "ship it",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "branch_kind": None,
                },
                {
                    "event_id": "event-2",
                    "cursor": "cursor-2",
                    "timestamp": "2026-07-12T12:00:01+00:00",
                    "role": "assistant",
                    "content_text": None,
                    "tool_name": "exec",
                    "tool_input_json": 'const r = await tools.write_stdin({"session_id": 17, "chars": ""}); text(JSON.stringify(r));',
                    "tool_output_text": None,
                    "tool_call_id": "call-2",
                    "branch_kind": None,
                }
            ],
            "next_cursor": "cursor-1",
            "has_more": True,
            "total": 75,
        }

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())
    read_args = {}

    def read_session(_session_id, **kwargs):
        read_args.update(kwargs)
        return session, None, "7"

    monkeypatch.setattr(workspace_module, "read_live_catalog_session", read_session)
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)

    result = await workspace_module.build_storage_v2_workspace(
        session_id=session_id,
        owner_id=42,
        branch_mode="head",
        limit=50,
    )

    assert result is not None
    assert result["projection"]["items"][0]["event"]["id"] == "event-1"
    wait_event = result["projection"]["items"][1]["event"]
    assert wait_event["tool_presentation"]["label"] == "Wait"
    assert wait_event["tool_presentation"]["wrapper_recedes"] is True
    assert result["projection"]["next_cursor"] == "cursor-1"
    assert result["projection"]["page_offset"] == 73
    assert result["workspace_revision"]["latest_event_id"] == "event-2"
    assert read_args == {"owner_id": 42}


@pytest.mark.asyncio
async def test_storage_v2_workspace_returns_none_for_legacy_session(monkeypatch):
    class MissingCatalog:
        async def call(self, method, params):
            return {"found": False, "deleted": False}

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: MissingCatalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _session_id, **_kwargs: (None, None, "0"))
    assert (
        await workspace_module.build_storage_v2_workspace(
            session_id=uuid4(),
            owner_id=42,
            branch_mode="head",
            limit=50,
        )
        is None
    )


@pytest.mark.asyncio
async def test_storage_v2_workspace_maps_catalog_read_outage_to_503(monkeypatch):
    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())

    def unavailable(*_args, **_kwargs):
        raise CatalogReadError("catalog_unavailable", "catalog unavailable")

    monkeypatch.setattr(workspace_module, "read_live_catalog_session", unavailable)
    with pytest.raises(workspace_module.HTTPException) as exc_info:
        await workspace_module.build_storage_v2_workspace(
            session_id=uuid4(),
            owner_id=42,
            branch_mode="head",
            limit=50,
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_storage_v2_workspace_keeps_live_control_only_session_openable(monkeypatch):
    session_id = uuid4()

    class MissingCatalog:
        async def call(self, method, params):
            return {"found": False, "deleted": False}

    session = SimpleNamespace(
        capabilities=SimpleNamespace(live_control_available=True),
        model_dump=lambda **_kwargs: {"id": str(session_id), "capabilities": {"live_control_available": True}},
    )
    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: MissingCatalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _session_id, **_kwargs: (session, None, "11"))

    result = await workspace_module.build_storage_v2_workspace(
        session_id=session_id,
        owner_id=42,
        branch_mode="head",
        limit=50,
    )

    assert result is not None
    assert result["control_only"] is True
    assert result["session"]["id"] == str(session_id)
    assert result["projection"]["items"] == []
    assert result["projection"]["total"] == 0


@pytest.mark.asyncio
async def test_empty_console_session_is_openable_before_archive_outbox_drains(monkeypatch, tmp_path):
    session_id = uuid4()
    thread_id = uuid4()
    engine = create_catalog_engine(tmp_path / "empty-console-read-after-write.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    with Session(engine) as db:
        db.add_all(
            [
                LiveUser(id=1, email="owner@example.com", is_active=True),
                LiveUser(id=42, email="other@example.com", is_active=True),
            ]
        )
        db.commit()
    store.create_console_session(
        data={
            "session_id": str(session_id),
            "thread_id": str(thread_id),
            "owner_id": 1,
            "provider": "codex",
            "device_id": "cinder",
            "cwd": "/tmp/longhouse",
            "project": "longhouse",
            "provider_config": {"permission_mode": "bypass"},
            "started_at": datetime.now(UTC),
        }
    )
    # Simulate a Console shell created by the prior release, before creation
    # also persisted the direct LiveSession ownership row. Its durable outbox
    # payload remains the compatibility source of ownership truth.
    with Session(engine) as db:
        db.query(LiveSession).filter(LiveSession.session_id == str(session_id)).delete()
        db.commit()

    class ArchiveProjection:
        ready = False

        async def call(self, method, params):
            assert method == "storage.session.read.v2"
            assert params == {"session_id": str(session_id)}
            if self.ready:
                return {
                    "found": True,
                    "commit_seq": "2",
                    "session": {"owner_id": "1", "updated_at": datetime.now(UTC).isoformat()},
                }
            return {"found": False, "deleted": False}

    archive = ArchiveProjection()
    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: archive)
    monkeypatch.setattr(
        "zerg.services.live_catalog_timeline.shadow_session_state_snapshot",
        lambda candidate, *, owner_id: store.read_shadow_session_state(session_id=candidate, owner_id=owner_id),
    )
    monkeypatch.setattr(
        "zerg.services.catalog_read_gateway.session_snapshot",
        lambda candidate, *, owner_id=None: store.read_session(session_id=candidate, owner_id=owner_id),
    )
    monkeypatch.setattr(
        "zerg.services.live_catalog_timeline.get_machine_control_channel_registry",
        lambda: SimpleNamespace(
            is_online=lambda **_kwargs: True,
            supports=lambda **_kwargs: True,
        ),
    )

    result = await workspace_module.build_storage_v2_workspace(
        session_id=session_id,
        owner_id=1,
        branch_mode="head",
        limit=50,
    )

    assert result is not None
    assert result["control_only"] is True
    assert result["session"]["origin_kind"] == "console"
    assert result["session"]["capabilities"]["composer_enabled"] is True, tuple(
        result["session"]["capabilities"].get(key)
        for key in ("control_label", "can_start_turn", "start_turn_blocked_by", "can_send_input", "input_mode")
    )
    assert result["session"]["capabilities"]["can_send_input"] is True
    assert result["session"]["capabilities"]["input_mode"] == "console"
    assert result["projection"]["items"] == []
    assert result["projection"]["total"] == 0

    async def archived_page(**_kwargs):
        return {
            "events": [
                {
                    "event_id": "event-1",
                    "cursor": "cursor-1",
                    "role": "assistant",
                    "content_text": "ready",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "branch_kind": "root",
                }
            ],
            "total": 1,
            "generation_id": "generation-1",
            "next_cursor": None,
            "has_more": False,
        }

    archive.ready = True
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", archived_page)
    converged = await workspace_module.build_storage_v2_workspace(
        session_id=session_id,
        owner_id=1,
        branch_mode="head",
        limit=50,
    )
    assert converged is not None
    assert converged["control_only"] is False
    assert converged["session"]["id"] == result["session"]["id"]
    assert converged["session"]["capabilities"] == result["session"]["capabilities"]
    assert len(converged["projection"]["items"]) == 1

    control_session = load_live_control_session_snapshot(session_id)
    assert control_session is not None
    assert control_session.command_family == "console_turn"

    dispatched = {}

    async def enqueue_console(**kwargs):
        dispatched.update(kwargs)
        return SimpleNamespace(turn_id=uuid4(), state="active", error=None)

    monkeypatch.setattr(session_chat_module, "enqueue_catalog_console_turn", enqueue_console)
    response = await session_chat_module._create_catalog_session_input_response(
        source_session=control_session,
        owner_id=1,
        body=session_chat_module.SessionInputRequest(text="first message", client_request_id="first-send"),
        db=None,
    )
    assert response.outcome == "sent"
    assert dispatched["session_id"] == session_id
    assert dispatched["message"] == "first message"

    assert (
        await workspace_module.build_storage_v2_workspace(
            session_id=session_id,
            owner_id=42,
            branch_mode="head",
            limit=50,
        )
        is None
    )

    with Session(engine) as db:
        catalog_session = db.get(LiveSessionCatalog, str(session_id))
        catalog_session.closed_at = datetime.now(UTC)
        catalog_session.close_reason = "user_closed"
        db.commit()
    closed = await workspace_module.build_storage_v2_workspace(
        session_id=session_id,
        owner_id=1,
        branch_mode="head",
        limit=50,
    )
    assert closed is not None
    assert closed["session"]["capabilities"]["composer_enabled"] is False
    assert closed["session"]["capabilities"]["can_send_input"] is False
