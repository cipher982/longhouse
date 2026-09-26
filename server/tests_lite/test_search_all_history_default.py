"""Typed search covers all indexed history by default; an explicit range narrows it.

Covers the fix for the gap where every search-shaped route quietly capped a
query at a recent window (web: 14 days default / 90 max; machine semantic and
recall: 365 max) even though nothing in the caller's request asked for that.
The archive FTS and the resident dense index both already answer an
unbounded query cheaply (a bounded newest-first candidate walk and a
vectorized in-memory scan respectively, see `agents_search.search_storage_v2_rows`
and `searchd/dense_index.py`), so the fix is only that the route layer must
stop inventing a cutoff when the caller did not name one.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from fastapi import Response

from zerg.routers import agents_search
from zerg.routers import agents_sessions
from zerg.routers import timeline
from zerg.services.session_listing import DEFAULT_LIST_DAYS_BACK
from zerg.services.session_listing import resolve_search_days_back
from zerg.services.timeline_session_listing import TimelineSessionsListResponse

# ---------------------------------------------------------------------------
# resolve_search_days_back: the one shared resolution rule
# ---------------------------------------------------------------------------


def test_resolve_search_days_back_explicit_value_always_narrows():
    assert resolve_search_days_back(30, has_query=True) == 30
    assert resolve_search_days_back(30, has_query=False) == 30


def test_resolve_search_days_back_defaults_to_all_history_for_a_query():
    assert resolve_search_days_back(None, has_query=True) is None


def test_resolve_search_days_back_defaults_to_recent_window_for_a_listing():
    assert resolve_search_days_back(None, has_query=False) == DEFAULT_LIST_DAYS_BACK == 14


# ---------------------------------------------------------------------------
# searchd lexical lane: no explicit range reaches searchd as no lower bound,
# not as a 14/90-day cutoff invented at the route.
# ---------------------------------------------------------------------------


def test_lexical_search_row_omits_window_start_when_days_back_is_none(monkeypatch):
    observed = {}

    class Client:
        async def call(self, method, params, **_kwargs):
            observed["method"] = method
            observed["params"] = params
            return {"results": []}

    monkeypatch.setattr(agents_search, "get_searchd_client", lambda: Client())

    asyncio.run(
        agents_search.search_storage_v2_rows(
            owner_id=7,
            query="pgvector migration",
            project=None,
            provider=None,
            environment=None,
            days_back=None,
            limit=20,
        )
    )

    assert observed["method"] == "search.query.v2"
    assert observed["params"]["window_start_us"] is None


def test_lexical_search_row_computes_window_start_when_days_back_is_explicit(monkeypatch):
    observed = {}

    class Client:
        async def call(self, method, params, **_kwargs):
            observed["params"] = params
            return {"results": []}

    monkeypatch.setattr(agents_search, "get_searchd_client", lambda: Client())

    asyncio.run(
        agents_search.search_storage_v2_rows(
            owner_id=7,
            query="pgvector migration",
            project=None,
            provider=None,
            environment=None,
            days_back=14,
            limit=20,
        )
    )

    assert observed["params"]["window_start_us"] is not None


# ---------------------------------------------------------------------------
# Machine surface: /api/agents/sessions and /api/agents/sessions/semantic
# ---------------------------------------------------------------------------


def test_agents_sessions_search_defaults_to_all_history_without_explicit_days_back(monkeypatch):
    observed = {}

    async def search_v2(**kwargs):
        observed.update(kwargs)
        return []

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
            days_back=None,
            query="pgvector migration",
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
    assert observed["days_back"] is None


def test_agents_sessions_listing_keeps_default_window_without_a_query(monkeypatch):
    observed = {}

    def fake_list_live_catalog_sessions(*, params, owner_id):
        observed["days_back"] = params.days_back
        return SimpleNamespace(sessions=[], total=0, has_real_sessions=False)

    monkeypatch.setattr(agents_sessions, "list_live_catalog_sessions", fake_list_live_catalog_sessions)

    asyncio.run(
        agents_sessions.list_sessions(
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=True,
            include_automation=False,
            device_id=None,
            days_back=None,
            query=None,
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

    assert observed["days_back"] == DEFAULT_LIST_DAYS_BACK


def test_semantic_machine_search_accepts_an_absent_days_back(monkeypatch):
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
            days_back=None,
            limit=10,
            context_mode="forensic",
            _auth=SimpleNamespace(owner_id=7),
            _single=None,
        )
    )

    assert response.total == 0
    assert observed["days_back"] is None


def test_recall_accepts_an_absent_since_days(monkeypatch):
    async def search_v2(**_kwargs):
        return []

    monkeypatch.setattr(agents_search, "search_storage_v2_rows", search_v2)

    request = agents_search.Request({"type": "http", "method": "GET", "path": "/api/agents/recall", "headers": [], "query_string": b""})
    response = asyncio.run(
        agents_search.recall_sessions(
            request=request,
            query="missing",
            project=None,
            provider=None,
            include_test=False,
            since_days=None,
            max_results=5,
            include_automation=False,
            mode="lexical",
            _auth=SimpleNamespace(owner_id=7),
            _single=None,
        )
    )

    assert response.total == 0


# ---------------------------------------------------------------------------
# Browser surface: /api/timeline/sessions
# ---------------------------------------------------------------------------


def test_timeline_sessions_search_defaults_to_all_history_without_explicit_days_back(monkeypatch):
    observed = {}

    async def fake_search_session_matches(**kwargs):
        observed.update(kwargs)
        return [], ["lexical"]

    async def fake_read_search_coverage(**_kwargs):
        return None

    monkeypatch.setattr(timeline._search_router, "search_session_matches", fake_search_session_matches)
    monkeypatch.setattr(timeline._search_router, "read_search_coverage", fake_read_search_coverage)
    monkeypatch.setattr(timeline, "_active_history_imports", lambda **_kwargs: [])

    result = asyncio.run(
        timeline.list_timeline_sessions(
            response=Response(),
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=True,
            include_automation=False,
            include_hidden=False,
            device_id=None,
            days_back=None,
            query="pgvector migration",
            limit=20,
            offset=0,
            sort=None,
            mode="lexical",
            context_mode="forensic",
            current_user=SimpleNamespace(owner_id=7),
        )
    )

    assert result.total == 0
    assert observed["days_back"] is None


def test_timeline_sessions_search_explicit_range_still_narrows(monkeypatch):
    observed = {}

    async def fake_search_session_matches(**kwargs):
        observed.update(kwargs)
        return [], ["lexical"]

    async def fake_read_search_coverage(**_kwargs):
        return None

    monkeypatch.setattr(timeline._search_router, "search_session_matches", fake_search_session_matches)
    monkeypatch.setattr(timeline._search_router, "read_search_coverage", fake_read_search_coverage)
    monkeypatch.setattr(timeline, "_active_history_imports", lambda **_kwargs: [])

    asyncio.run(
        timeline.list_timeline_sessions(
            response=Response(),
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=True,
            include_automation=False,
            include_hidden=False,
            device_id=None,
            days_back=30,
            query="pgvector migration",
            limit=20,
            offset=0,
            sort=None,
            mode="lexical",
            context_mode="forensic",
            current_user=SimpleNamespace(owner_id=7),
        )
    )

    assert observed["days_back"] == 30


def test_timeline_sessions_listing_keeps_default_window_without_a_query(monkeypatch):
    observed = {}

    def fake_list_live_catalog_timeline(*, params, owner_id):
        observed["days_back"] = params.days_back
        return TimelineSessionsListResponse(sessions=[], total=0, has_real_sessions=False)

    monkeypatch.setattr(timeline, "list_live_catalog_timeline", fake_list_live_catalog_timeline)
    monkeypatch.setattr(timeline, "_active_history_imports", lambda **_kwargs: [])

    asyncio.run(
        timeline.list_timeline_sessions(
            response=Response(),
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=True,
            include_automation=False,
            include_hidden=False,
            device_id=None,
            days_back=None,
            query=None,
            limit=20,
            offset=0,
            sort=None,
            mode="lexical",
            context_mode="forensic",
            current_user=SimpleNamespace(owner_id=7),
        )
    )

    assert observed["days_back"] == DEFAULT_LIST_DAYS_BACK
