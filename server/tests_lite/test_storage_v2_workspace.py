from types import SimpleNamespace
from uuid import uuid4

import pytest

import zerg.services.session_workspace as session_workspace_module
import zerg.services.storage_v2_workspace as workspace_module


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
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True),
        model_dump=lambda **_kwargs: {"id": str(session_id), "lifecycle": "open", "capabilities": {}},
    )

    async def read_page(**kwargs):
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
                }
            ],
            "next_cursor": "cursor-1",
            "has_more": True,
            "total": 75,
        }

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: _Catalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _session_id: (session, None, "7"))
    monkeypatch.setattr(workspace_module, "read_storage_v2_session_events_page", read_page)

    result = await workspace_module.build_storage_v2_workspace(
        session_id=session_id,
        owner_id=42,
        branch_mode="head",
        limit=50,
    )

    assert result is not None
    assert result["projection"]["items"][0]["event"]["id"] == "event-1"
    assert result["projection"]["next_cursor"] == "cursor-1"
    assert result["projection"]["page_offset"] == 74
    assert result["workspace_revision"]["latest_event_id"] == "event-1"


@pytest.mark.asyncio
async def test_storage_v2_workspace_returns_none_for_legacy_session(monkeypatch):
    class MissingCatalog:
        async def call(self, method, params):
            return {"found": False, "deleted": False}

    monkeypatch.setattr(workspace_module, "get_catalogd_client", lambda: MissingCatalog())
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _session_id: (None, None, "0"))
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
    monkeypatch.setattr(workspace_module, "read_live_catalog_session", lambda _session_id: (session, None, "11"))

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
