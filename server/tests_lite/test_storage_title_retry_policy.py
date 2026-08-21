"""Bounded storage-v2 AI title retry policy."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import select

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import MAX_TITLE_ATTEMPTS
from zerg.catalogd.store import CatalogStore
from zerg.catalogd.store import StorageSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.session_title import is_resume_seed_marker


@pytest.mark.asyncio
async def test_storage_title_allows_path_prompt_to_reach_model(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    calls: list[tuple[str, dict]] = []
    client = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(llm_disabled=False)

    async def _fake_catalog_call(method, params):
        calls.append((method, params))
        if method == "storage.session.title.dependency.acquire.v2":
            return {"allowed": True, "incident_id": None}
        if method == "storage.session.title.dependency.reconcile.v2":
            return {"dependency": {"state": "healthy"}}
        return {"changed": True}

    async def _fake_generate_initial_session_title(**kwargs):
        assert kwargs["timeout_seconds"] == 15.0
        return "Provider Factory Audit"

    monkeypatch.setattr(storage_titles, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    monkeypatch.setattr(storage_titles, "_dependency_identity", lambda: _dependency_identity())
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
    assert calls[-1][0] == "storage.session.title.complete.v2"
    assert calls[-1][1]["title"] == "Provider Factory Audit"
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_title_credential_opens_dependency_incident_without_spending_row_attempt(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    calls: list[tuple[str, dict]] = []
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(storage_titles, "get_settings", lambda: SimpleNamespace(llm_disabled=False))

    async def _fake_catalog_call(method, params):
        calls.append((method, params))
        if method == "storage.session.title.dependency.acquire.v2":
            return {"allowed": True, "incident_id": None}
        if method == "storage.session.title.dependency.fail.v2":
            return {"attempt_consumed": False, "incident_id": str(uuid4())}
        return {"changed": True}

    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    generated = await storage_titles.generate_storage_session_title(
        {
            "session_id": str(uuid4()),
            "first_user_message": "Explain why the title credential is missing",
            "provider": "opencode",
            "project": "longhouse",
            "git_branch": "main",
        }
    )

    assert generated is False
    methods = [method for method, _params in calls]
    assert methods == [
        "storage.session.title.dependency.reconcile.v2",
        "storage.session.title.dependency.acquire.v2",
        "storage.session.title.dependency.fail.v2",
    ]
    identity = calls[0][1]
    assert identity["credential_binding"] == "OPENROUTER_API_KEY"
    assert len(identity["credential_generation"]) == 64


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


def _insert_visible_live_catalog(engine, *, session_id, provider="claude"):
    now = datetime.now(UTC).replace(microsecond=0)
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=str(session_id),
                provider=provider,
                environment="local",
                project="longhouse",
                device_id="cinder",
                started_at=now,
                last_activity_at=now,
                user_messages=1,
                hidden_from_default_timeline=0,
                created_at=now,
                updated_at=now,
            )
        )


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
            assert result["retry_at"] is not None
            assert result["title"] is None  # deterministic fallback is not an AI anchor

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


def test_candidate_listing_keeps_visible_claude_raw_until_semantic_repair(tmp_path):
    engine = _build_engine(tmp_path)
    session_id = uuid4()
    _insert_session(
        engine,
        session_id=session_id,
        provider="claude",
        first_message="Reply with exactly LONGHOUSE_CLAUDE_TURN_BOUNDARY_abc123",
    )
    _insert_visible_live_catalog(engine, session_id=session_id)

    assert str(session_id) not in _candidate_ids(engine)


def test_is_resume_seed_marker_matches_only_well_formed_token():
    assert is_resume_seed_marker("LONGHOUSE_OPENCODE_RESUME_SEED_abc123") is True
    assert is_resume_seed_marker("LONGHOUSE_CODEX_COLD_RESUME_SEED_abc123") is True
    assert is_resume_seed_marker(None) is False
    assert is_resume_seed_marker("") is False
    assert is_resume_seed_marker("please resume the stalled deploy") is False
    assert is_resume_seed_marker("RESUME_SEED is not how people talk") is False


def _dependency_identity(generation: str = "a" * 64) -> dict[str, str]:
    return {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash-0731",
        "credential_binding": "OPENROUTER_API_KEY",
        "credential_generation": generation,
    }


def _title_row(engine, session_id):
    with engine.connect() as connection:
        return connection.execute(
            select(StorageSession.__table__).where(StorageSession.__table__.c.session_id == str(session_id))
        ).mappings().one()


def test_dependency_401_coalesces_multi_row_incident_without_spending_attempts(tmp_path):
    engine = _build_engine(tmp_path)
    first_id = uuid4()
    second_id = uuid4()
    _insert_session(engine, session_id=first_id, first_message="fix the first bug")
    _insert_session(engine, session_id=second_id, first_message="fix the second bug")
    store = CatalogStore(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    identity = _dependency_identity()
    store.reconcile_storage_title_dependency(**identity, observed_at=now)

    first_token = uuid4()
    second_token = uuid4()
    assert store.acquire_storage_title_dependency(
        session_id=first_id,
        probe_token=first_token,
        observed_at=now,
        lease_seconds=60,
        **identity,
    )["allowed"] is True
    assert store.acquire_storage_title_dependency(
        session_id=second_id,
        probe_token=second_token,
        observed_at=now,
        lease_seconds=60,
        **identity,
    )["allowed"] is True

    first = store.fail_storage_title_dependency(
        session_id=first_id,
        probe_token=first_token,
        reason="Error code: 401 - invalid API key",
        failed_at=now,
        **identity,
    )
    second = store.fail_storage_title_dependency(
        session_id=second_id,
        probe_token=second_token,
        reason="AuthenticationError: 401",
        failed_at=now,
        **identity,
    )

    assert first["new_incident"] is True
    assert second["new_incident"] is False
    assert first["incident_id"] == second["incident_id"]
    assert _title_row(engine, first_id)["title_attempt_count"] == 0
    assert _title_row(engine, second_id)["title_attempt_count"] == 0
    assert _title_row(engine, first_id)["title_dependency_incident_id"] == first["incident_id"]
    assert _title_row(engine, second_id)["title_dependency_incident_id"] == first["incident_id"]
    health = store.read_storage_title_dependency_health()
    assert health["open_dependencies"] == 1
    assert health["blocked_sessions"] == 2


def test_dependency_incident_survives_restart_and_exact_replay(tmp_path):
    db_path = tmp_path / "catalog.db"
    engine = create_catalog_engine(db_path)
    initialize_catalog_schema(engine)
    session_id = uuid4()
    _insert_session(engine, session_id=session_id, first_message="give this a title")
    now = datetime.now(UTC).replace(microsecond=0)
    identity = _dependency_identity()
    store = CatalogStore(engine)
    store.reconcile_storage_title_dependency(**identity, observed_at=now)
    token = uuid4()
    failed = store.fail_storage_title_dependency(
        session_id=session_id,
        probe_token=token,
        reason="401 unauthorized",
        failed_at=now,
        **identity,
    )
    engine.dispose()

    fresh_engine = create_catalog_engine(db_path)
    initialize_catalog_schema(fresh_engine)
    fresh = CatalogStore(fresh_engine)
    replay = fresh.fail_storage_title_dependency(
        session_id=session_id,
        probe_token=token,
        reason="401 unauthorized",
        failed_at=now,
        **identity,
    )
    reconciled = fresh.reconcile_storage_title_dependency(**identity, observed_at=now + timedelta(seconds=1))

    assert replay["incident_id"] == failed["incident_id"]
    assert reconciled["dependency"]["incident_id"] == failed["incident_id"]
    assert reconciled["dependency"]["state"] == "open"
    assert _title_row(fresh_engine, session_id)["title_attempt_count"] == 0


def test_dependency_recovery_rearms_only_incident_bound_terminal_debt(tmp_path):
    engine = _build_engine(tmp_path)
    auth_id = uuid4()
    unrelated_id = uuid4()
    _insert_session(engine, session_id=auth_id, first_message="auth debt")
    _insert_session(engine, session_id=unrelated_id, first_message="timeout debt")
    store = CatalogStore(engine)
    now = (datetime.now(UTC) - timedelta(seconds=5)).replace(microsecond=0)
    for _ in range(MAX_TITLE_ATTEMPTS):
        store.fail_storage_title(session_id=auth_id, reason="AuthenticationError: 401", failed_at=now)
        store.fail_storage_title(session_id=unrelated_id, reason="TimeoutError", failed_at=now)

    identity_a = _dependency_identity("a" * 64)
    adopted = store.reconcile_storage_title_dependency(**identity_a, observed_at=now)
    incident_id = adopted["dependency"]["incident_id"]
    assert incident_id
    assert _title_row(engine, auth_id)["title_dependency_incident_id"] == incident_id

    identity_b = _dependency_identity("b" * 64)
    store.reconcile_storage_title_dependency(**identity_b, observed_at=now + timedelta(seconds=1))
    assert str(auth_id) in _candidate_ids(engine)
    token = uuid4()
    acquired = store.acquire_storage_title_dependency(
        session_id=auth_id,
        probe_token=token,
        observed_at=now + timedelta(seconds=1),
        lease_seconds=60,
        **identity_b,
    )
    assert acquired["allowed"] is True
    recovered = store.recover_storage_title_dependency(
        incident_id=UUID(incident_id),
        probe_token=token,
        recovered_at=now + timedelta(seconds=2),
        **identity_b,
    )

    assert recovered["rearmed_sessions"] == 1
    auth_row = _title_row(engine, auth_id)
    unrelated_row = _title_row(engine, unrelated_id)
    assert auth_row["title_attempt_count"] == 0
    assert auth_row["title_dependency_incident_id"] is None
    assert auth_row["title_last_error"] is None
    assert unrelated_row["title_attempt_count"] == MAX_TITLE_ATTEMPTS
    assert unrelated_row["title_last_error"] == "TimeoutError"
    assert str(auth_id) in _candidate_ids(engine)
    assert str(unrelated_id) not in _candidate_ids(engine)
