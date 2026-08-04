from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import Response
from starlette.requests import Request

from zerg.routers import agents_search
from zerg.routers import agents_sessions
from zerg.routers import timeline
from zerg.services.session_views import RecallMatch
from zerg.services.session_views import RecallResponse


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


def _fail_legacy_factory():
    raise AssertionError("storage-v2 machine search must not open DATABASE_URL")


def _coverage() -> dict[str, object]:
    return {
        "ready": True,
        "projector": "embeddings-test-256d-p2",
        "cutover_certified_commit_seq": "9",
        "cutover_certified_at": "2026-08-02T00:00:00+00:00",
        "search_store_id": "00000000-0000-4000-8000-000000000001",
        "search_schema_generation": "searchd-test-v1",
        "catalog_lag_count": 0,
        "catalog_indexed_through": "10",
        "catalog_oldest_lag_at": None,
        "catalog_oldest_lag_seconds": None,
        "catalog_commit_seq": "10",
        "catalog_observed_at": "2026-08-02T00:00:00+00:00",
        "resident_stale": False,
        "expected_sessions": 1,
        "published_sessions": 1,
        "expected_episodes": 1,
        "current_episodes": 1,
        "invalid_vectors": 0,
        "unnormalized_vectors": 0,
        "unlocatable_episodes": 0,
        "episode_count_mismatches": 0,
        "missing_session_ids": [],
    }


def test_recall_contract_accepts_evaluator_depth():
    app = FastAPI()
    app.include_router(agents_search.router)
    operation = app.openapi()["paths"]["/agents/recall"]["get"]
    max_results = next(parameter for parameter in operation["parameters"] if parameter["name"] == "max_results")

    assert max_results["schema"]["maximum"] == 25


def test_recall_discovery_uses_hydration_reserve_only_when_requested():
    assert agents_search._discovery_budget(remaining_seconds=1.9, context_turns=0) == 1.9
    assert agents_search._discovery_budget(remaining_seconds=1.9, context_turns=2) == pytest.approx(0.9)


def test_semantic_machine_search_uses_searchd_without_legacy_db(monkeypatch):
    observed = {}

    async def search_v2(**kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(agents_search, "search_storage_v2_semantic_sessions", search_v2)

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
            _auth=SimpleNamespace(owner_id=7),
            _single=None,
        )
    )

    assert response.total == 0
    assert observed["owner_id"] == 7
    assert observed["query"] == "database migration"
    assert observed["environment"] is None


def test_storage_v2_machine_search_hydrates_hits_with_owner_scope(monkeypatch):
    session_id = "11111111-1111-4111-8111-111111111111"
    observed = {}

    async def search_v2(**kwargs):
        assert kwargs["owner_id"] == 7
        return [{"session_id": session_id, "content_snippet": "scoped hit"}]

    def read_session(requested, *, owner_id):
        observed.update(requested=requested, owner_id=owner_id)
        return None, None, "9"

    monkeypatch.setattr(agents_search, "search_storage_v2_rows", search_v2)
    monkeypatch.setattr(agents_search, "read_live_catalog_session", read_session)

    result = asyncio.run(
        agents_search.search_storage_v2_sessions(
            owner_id=7,
            query="scoped hit",
            project=None,
            provider=None,
            environment=None,
            days_back=14,
            limit=10,
            include_test=False,
        )
    )

    assert result == []
    assert observed == {"requested": agents_search.UUID(session_id), "owner_id": 7}


def test_recall_machine_search_uses_searchd_without_legacy_db(monkeypatch):
    observed = {}

    async def search_v2(**kwargs):
        observed.update(kwargs)
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

    monkeypatch.setattr(agents_search, "search_storage_v2_rows", search_v2)

    async def context_v2(**kwargs):
        assert kwargs["search_event_id"] == 9
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "total_events": 12,
            "context": [
                {
                    "search_event_id": 9,
                    "event_id": "event-9",
                    "source_object_id": "a" * 64,
                    "record_ordinal": 4,
                    "order_time_us": 100,
                    "role": "user",
                    "content_text": "please migrate",
                    "tool_name": None,
                }
            ],
            "timing": {"admit_ms": 0.0, "sql_ms": 0.1, "active_readers": 1, "queued_readers": 0},
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", context_v2)
    monkeypatch.setattr(agents_search, "_server_build_commit", lambda: "c" * 40)

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
            mode="lexical",
            _auth=SimpleNamespace(owner_id=7),
            _single=None,
        )
    )

    assert response.total == 1
    assert response.matches[0].evidence == "the migration completed"
    assert response.matches[0].total_events == 12
    assert response.matches[0].context[0]["content_text"] == "please migrate"
    assert response.server_commit == "c" * 40
    assert observed["include_snippets"] is False


def test_recall_rejects_unknown_query_parameters_before_search():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/agents/recall",
            "headers": [],
            "query_string": b"query=migration&limti=10",
        }
    )

    with pytest.raises(agents_search.HTTPException) as exc_info:
        asyncio.run(
            agents_search.recall_sessions(
                request=request,
                query="migration",
                project=None,
                provider=None,
                include_test=False,
                since_days=90,
                max_results=5,
                context_turns=2,
                context_mode="forensic",
                include_automation=False,
                mode="lexical",
                _auth=SimpleNamespace(owner_id=7),
                _single=None,
            )
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["parameters"] == ["limti"]


def test_browser_recall_delegates_to_requested_canonical_pipeline(monkeypatch):
    observed = {}

    async def canonical_recall(**kwargs):
        observed.update(kwargs)
        return RecallResponse(
            matches=[],
            total=0,
            lanes=["lexical", "dense"],
            embedding_model="google/embeddinggemma-300m",
            embedding_dims=256,
            embedding_revision="a" * 40,
            coverage=_coverage(),
        )

    monkeypatch.setattr(timeline._search_router, "recall_sessions", canonical_recall)
    response = asyncio.run(
        timeline.recall_timeline_sessions(
            request=_request("/api/timeline/recall"),
            response=Response(),
            query="database migration",
            project=None,
            provider=None,
            include_test=False,
            since_days=90,
            max_results=5,
            context_turns=2,
            context_mode="forensic",
            mode="lexical",
            current_user=SimpleNamespace(id=7),
        )
    )

    assert response.lanes == ["lexical", "dense"]
    assert observed["mode"] == "lexical"
    assert observed["include_automation"] is False
    assert observed["_auth"].owner_id == 7


def test_semantic_session_search_overfetches_and_filters_hidden_autonomous_hits(monkeypatch):
    observed = {}
    session_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 6)]

    async def dense_matches(**kwargs):
        observed.update(kwargs)
        return [RecallMatch(session_id=session_id, chunk_index=0, score=1.0 - index / 10) for index, session_id in enumerate(session_ids)]

    class FakeSession:
        def __init__(self, session_id, *, hidden=False, user_messages=1, sidechain=False):
            self.id = session_id
            self.user_hidden_from_timeline = hidden
            self.user_messages = user_messages
            self.is_sidechain = sidechain
            self.environment = "production"

        def model_copy(self, *, update):
            self.match_score = update["match_score"]
            return self

    sessions = {
        session_ids[0]: FakeSession(session_ids[0], hidden=True),
        session_ids[1]: FakeSession(session_ids[1], user_messages=0),
        session_ids[2]: FakeSession(session_ids[2], sidechain=True),
        session_ids[3]: FakeSession(session_ids[3]),
        session_ids[4]: FakeSession(session_ids[4]),
    }

    def read_session(session_id, *, owner_id):
        assert owner_id == 7
        return sessions[str(session_id)], None, "10"

    monkeypatch.setattr(agents_search, "_semantic_recall_matches", dense_matches)
    monkeypatch.setattr(agents_search, "read_live_catalog_session", read_session)

    result = asyncio.run(
        agents_search.search_storage_v2_semantic_sessions(
            owner_id=7,
            query="migration",
            project=None,
            provider=None,
            environment=None,
            days_back=14,
            limit=2,
            include_test=False,
        )
    )

    assert observed["max_results"] == 10
    assert [session.id for session in result] == session_ids[-2:]


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
