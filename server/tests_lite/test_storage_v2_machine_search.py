from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from zerg.routers import agents_search
from zerg.routers import agents_sessions


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


def _fail_legacy_factory():
    raise AssertionError("storage-v2 machine search must not open DATABASE_URL")


def test_semantic_machine_search_uses_searchd_without_legacy_db(monkeypatch):
    observed = {}

    async def search_v2(**kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(agents_search.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_search.database_module, "get_session_factory", _fail_legacy_factory)
    monkeypatch.setattr(agents_search, "search_storage_v2_sessions", search_v2)

    response = asyncio.run(
        agents_search.semantic_search_sessions(
            query="database migration",
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            days_back=14,
            limit=10,
            context_mode="forensic",
            db=None,
            _auth=SimpleNamespace(owner_id=7),
            _single=None,
        )
    )

    assert response.total == 0
    assert observed["owner_id"] == 7
    assert observed["query"] == "database migration"


def test_recall_machine_search_uses_searchd_without_legacy_db(monkeypatch):
    async def search_v2(**_kwargs):
        return [
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "generation_id": "22222222-2222-4222-8222-222222222222",
                "search_event_id": 9,
                "record_ordinal": 4,
                "content_snippet": "the migration completed",
                "environment": "production",
                "rank": -2.0,
            }
        ]

    monkeypatch.setattr(agents_search.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_search.database_module, "get_session_factory", _fail_legacy_factory)
    monkeypatch.setattr(agents_search, "search_storage_v2_rows", search_v2)

    async def context_v2(**kwargs):
        assert kwargs["search_event_id"] == 9
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "total_events": 12,
            "context": [{"role": "user", "content_text": "please migrate"}],
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", context_v2)

    response = asyncio.run(
        agents_search.recall_sessions(
            request=_request("/api/agents/recall"),
            query="migration",
            project=None,
            provider=None,
            include_test=False,
            since_days=90,
            max_results=5,
            context_turns=2,
            context_mode="forensic",
            include_automation=False,
            mode="auto",
            database_url=None,
            session_factory=None,
            _auth=SimpleNamespace(owner_id=7),
            _single=None,
        )
    )

    assert response.total == 1
    assert response.matches[0].evidence == "the migration completed"
    assert response.matches[0].total_events == 12
    assert response.matches[0].context[0]["content_text"] == "please migrate"


def test_machine_session_list_query_uses_searchd_without_legacy_db(monkeypatch):
    observed = {}

    async def search_v2(**kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions.database_module, "get_session_factory", _fail_legacy_factory)
    monkeypatch.setattr(agents_sessions, "search_storage_v2_sessions", search_v2)

    response = asyncio.run(
        agents_sessions.list_sessions(
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=True,
            include_automation=False,
            device_id=None,
            days_back=14,
            query="storage v2",
            limit=20,
            offset=0,
            sort=None,
            mode="lexical",
            context_mode="forensic",
            db=None,
            _auth=SimpleNamespace(owner_id=9),
            _single=None,
        )
    )

    assert response.total == 0
    assert observed["owner_id"] == 9


def test_retired_recall_index_is_typed_in_catalog_mode(monkeypatch):
    monkeypatch.setattr(agents_search.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_search.database_module, "get_session_factory", _fail_legacy_factory)

    status = asyncio.run(agents_search.recall_index_status(database_url=None, _auth=None, _single=None))
    assert status == {"status": "retired", "reason": "storage_v2_search_owned"}

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            agents_search.index_recall_sessions(
                project=None,
                provider=None,
                since_days=90,
                limit=100,
                database_url=None,
                _auth=None,
                _single=None,
            )
        )
    assert error.value.status_code == 410
    assert error.value.detail["code"] == "recall_index_retired"
