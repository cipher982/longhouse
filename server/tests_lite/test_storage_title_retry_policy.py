"""Bounded storage-v2 AI title retry policy.

Regression coverage for the runaway title loop: a title generation that keeps
timing out must stop after a small bounded number of attempts, freeze the
deterministic non-AI fallback title, and persist that terminal state so a
restart cannot resume the loop. Also covers the assurance-harness seed-marker
skip.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import MAX_TITLE_ATTEMPTS
from zerg.catalogd.store import CatalogStore
from zerg.catalogd.store import StorageSession
from zerg.services.session_title import is_resume_seed_marker


@pytest.mark.asyncio
async def test_storage_title_allows_path_prompt_to_reach_model(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    calls: list[tuple[str, dict]] = []
    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(llm_disabled=False)

    async def _fake_catalog_call(method, params):
        calls.append((method, params))
        return {"changed": True}

    async def _fake_generate_initial_session_title(**_kwargs):
        return "Provider Factory Audit"

    monkeypatch.setattr(storage_titles, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    monkeypatch.setattr("zerg.models_config.get_llm_client_for_use_case", lambda _use_case: (client, "test-model", "test"))
    monkeypatch.setattr("zerg.services.title_generator.generate_initial_session_title", _fake_generate_initial_session_title)

    generated = await storage_titles.generate_storage_session_title(
        {
            "session_id": str(uuid4()),
            "first_user_message": "/Users/davidrose/git/obsidian_vault/AI-Sessions/provider-factory-audit.md",
            "provider": "claude",
            "project": "longhouse",
            "git_branch": "main",
        }
    )

    assert generated is True
    assert calls[0][0] == "storage.session.title.complete.v2"
    assert calls[0][1]["title"] == "Provider Factory Audit"
    client.close.assert_awaited_once()


def _insert_session(engine, *, session_id, first_message, environment="local", provider="opencode"):
    now = datetime.now(UTC).replace(microsecond=0)
    with engine.begin() as connection:
        connection.execute(
            StorageSession.__table__.insert().values(
                session_id=str(session_id),
                tenant_id="tenant-a",
                owner_id="42",
                provider=provider,
                environment=environment,
                machine_id="cinder",
                project="zerg",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                first_user_message_preview=first_message,
                commit_seq=1,
                created_at=now,
                updated_at=now,
            )
        )


def _candidate_ids(engine) -> set[str]:
    store = CatalogStore(engine)
    result = store.list_storage_title_candidates(limit=100)
    return {str(candidate["session_id"]) for candidate in result.get("sessions", [])}


def _build_engine(root: Path):
    engine = create_catalog_engine(root / "catalog.db")
    initialize_catalog_schema(engine)
    return engine


def test_retrying_title_times_out_bounded_then_persists_terminal(tmp_path):
    engine = _build_engine(tmp_path)
    session_id = uuid4()
    first_message = "fix the refresh token bug"
    _insert_session(engine, session_id=session_id, first_message=first_message)
    store = CatalogStore(engine)
    now = datetime.now(UTC)

    for attempt in range(1, MAX_TITLE_ATTEMPTS + 1):
        result = store.fail_storage_title(
            session_id=session_id,
            reason="TimeoutError",
            failed_at=now,
        )
        assert result["changed"] is True
        assert result["attempt_count"] == attempt
        if attempt < MAX_TITLE_ATTEMPTS:
            assert result["terminal"] is not True
            assert result["title"] is None
            assert result["retry_at"] is not None  # keeps backing off, not fixed cadence
        else:
            assert result["terminal"] is True
            assert result["retry_at"] is None
            assert result["title"] == first_message  # deterministic non-AI fallback

    assert str(session_id) not in _candidate_ids(engine)

    sixth = store.fail_storage_title(session_id=session_id, reason="TimeoutError", failed_at=now)
    assert sixth["changed"] is False  # terminal state: no further attempt counting


def test_terminal_state_survives_restart(tmp_path):
    db_path = tmp_path / "catalog.db"
    engine = create_catalog_engine(db_path)
    initialize_catalog_schema(engine)
    session_id = uuid4()
    _insert_session(engine, session_id=session_id, first_message="keep timing out forever")
    store = CatalogStore(engine)
    now = datetime.now(UTC)
    for _ in range(MAX_TITLE_ATTEMPTS):
        store.fail_storage_title(session_id=session_id, reason="TimeoutError", failed_at=now)
    engine.dispose()

    fresh = create_catalog_engine(db_path)
    initialize_catalog_schema(fresh)
    assert str(session_id) not in _candidate_ids(fresh)


def test_candidate_listing_skips_seed_marker_and_over_budget_sessions(tmp_path):
    engine = _build_engine(tmp_path)
    seed_id = uuid4()
    normal_id = uuid4()
    burnt_id = uuid4()
    _insert_session(
        engine,
        session_id=seed_id,
        first_message="LONGHOUSE_OPENCODE_RESUME_SEED_3138112c55bf40b494f58bd8d804d973",
    )
    _insert_session(engine, session_id=normal_id, first_message="debug the sse frame drop")
    _insert_session(engine, session_id=burnt_id, first_message="already at the retry cap")

    store = CatalogStore(engine)
    now = datetime.now(UTC)
    for _ in range(MAX_TITLE_ATTEMPTS):
        store.fail_storage_title(session_id=burnt_id, reason="TimeoutError", failed_at=now + timedelta(seconds=1))

    candidates = _candidate_ids(engine)
    assert str(normal_id) in candidates
    assert str(seed_id) not in candidates
    assert str(burnt_id) not in candidates


def test_is_resume_seed_marker_matches_only_well_formed_token():
    assert is_resume_seed_marker("LONGHOUSE_OPENCODE_RESUME_SEED_abc123") is True
    assert is_resume_seed_marker("LONGHOUSE_CODEX_COLD_RESUME_SEED_abc123") is True
    assert is_resume_seed_marker(None) is False
    assert is_resume_seed_marker("") is False
    assert is_resume_seed_marker("please resume the stalled deploy") is False
    assert is_resume_seed_marker("RESUME_SEED is not how people talk") is False
