"""Bounded storage-v2 AI title retry policy."""

from __future__ import annotations

import asyncio
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
from sqlalchemy import update

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import MAX_TITLE_ATTEMPTS
from zerg.catalogd.store import TITLE_ROW_TRANSIENT_RETRY_DELAY
from zerg.catalogd.store import CatalogStore
from zerg.catalogd.store import StorageSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.session_title import is_resume_seed_marker


def _authorized_candidate(**values):
    return {**values, "canonical_title_eligible": True}


@pytest.mark.asyncio
async def test_title_worker_requires_canonical_catalog_eligibility(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    monkeypatch.setattr(storage_titles, "get_settings", lambda: SimpleNamespace(llm_disabled=False))
    generated = await storage_titles.generate_storage_session_title(
        {"session_id": str(uuid4()), "first_user_message": "Untrusted direct schedule", "provider": "claude"}
    )

    assert generated is False


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
        assert kwargs["timeout_seconds"] == 30.0
        return "Provider Factory Audit"

    monkeypatch.setattr(storage_titles, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    monkeypatch.setattr(storage_titles, "_dependency_identity", lambda: _dependency_identity())
    monkeypatch.setattr("zerg.models_config.get_llm_client_for_use_case", lambda _use_case: (client, "test-model", "test"))
    monkeypatch.setattr("zerg.services.title_generator.generate_initial_session_title", _fake_generate_initial_session_title)

    generated = await storage_titles.generate_storage_session_title(
        _authorized_candidate(
            session_id=str(uuid4()),
            first_user_message="/Users/davidrose/git/obsidian_vault/AI-Sessions/provider-factory-audit.md",
            provider="claude",
            project="longhouse",
            git_branch="main",
        )
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
        _authorized_candidate(
            session_id=str(uuid4()),
            first_user_message="Explain why the title credential is missing",
            provider="opencode",
            project="longhouse",
            git_branch="main",
        )
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
    assert calls[-1][1]["failure_class"] == "authentication"


@pytest.mark.asyncio
async def test_provider_timeout_opens_availability_incident_without_row_failure(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    calls: list[tuple[str, dict]] = []
    client = SimpleNamespace(close=AsyncMock())

    async def _fake_catalog_call(method, params):
        calls.append((method, params))
        if method == "storage.session.title.dependency.acquire.v2":
            return {"allowed": True, "incident_id": None}
        return {"changed": True}

    async def _timeout(**_kwargs):
        raise TimeoutError("title provider timed out")

    monkeypatch.setattr(storage_titles, "get_settings", lambda: SimpleNamespace(llm_disabled=False))
    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    monkeypatch.setattr(storage_titles, "_dependency_identity", lambda: _dependency_identity())
    monkeypatch.setattr("zerg.models_config.get_llm_client_for_use_case", lambda _use_case: (client, "test-model", "test"))
    monkeypatch.setattr("zerg.services.title_generator.generate_initial_session_title", _timeout)

    generated = await storage_titles.generate_storage_session_title(
        _authorized_candidate(
            session_id=str(uuid4()), first_user_message="Explain the provider timeout", provider="opencode"
        )
    )

    assert generated is False
    assert [method for method, _params in calls][-1] == "storage.session.title.dependency.fail.v2"
    assert calls[-1][1]["failure_class"] == "availability"
    assert "storage.session.title.fail.v2" not in [method for method, _params in calls]


@pytest.mark.asyncio
async def test_catalog_timeout_does_not_poison_provider_or_spend_row_attempt(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    calls: list[str] = []

    async def _catalog_timeout(method, _params):
        calls.append(method)
        raise TimeoutError("catalog writer is busy")

    monkeypatch.setattr(storage_titles, "get_settings", lambda: SimpleNamespace(llm_disabled=False))
    monkeypatch.setattr(storage_titles, "_catalog_call", _catalog_timeout)
    monkeypatch.setattr(storage_titles, "_dependency_identity", lambda: _dependency_identity())

    generated = await storage_titles.generate_storage_session_title(
        _authorized_candidate(
            session_id=str(uuid4()), first_user_message="Explain the catalog timeout", provider="opencode"
        )
    )

    assert generated is False
    assert calls == ["storage.session.title.dependency.reconcile.v2"]


@pytest.mark.asyncio
async def test_empty_model_response_remains_row_scoped(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    calls: list[tuple[str, dict]] = []
    client = SimpleNamespace(close=AsyncMock())
    incident_id = str(uuid4())

    async def _fake_catalog_call(method, params):
        calls.append((method, params))
        if method == "storage.session.title.dependency.acquire.v2":
            return {"allowed": True, "incident_id": incident_id}
        return {"changed": True}

    async def _empty(**_kwargs):
        return ""

    monkeypatch.setattr(storage_titles, "get_settings", lambda: SimpleNamespace(llm_disabled=False))
    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    monkeypatch.setattr(storage_titles, "_dependency_identity", lambda: _dependency_identity())
    monkeypatch.setattr("zerg.models_config.get_llm_client_for_use_case", lambda _use_case: (client, "test-model", "test"))
    monkeypatch.setattr("zerg.services.title_generator.generate_initial_session_title", _empty)

    generated = await storage_titles.generate_storage_session_title(
        _authorized_candidate(session_id=str(uuid4()), first_user_message="Return a useful title", provider="opencode")
    )

    assert generated is False
    assert [method for method, _params in calls][-1] == "storage.session.title.fail.v2"
    assert "storage.session.title.dependency.recover.v2" in [method for method, _params in calls]
    assert "storage.session.title.dependency.fail.v2" not in [method for method, _params in calls]


@pytest.mark.asyncio
async def test_scheduler_reserves_four_slots_before_creating_tasks_and_drains_backlog(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    await storage_titles.stop_storage_title_workers()
    monkeypatch.setattr(storage_titles, "_model_slots", asyncio.Semaphore(storage_titles.STORAGE_TITLE_MAX_CONCURRENCY))
    monkeypatch.setattr(storage_titles, "get_settings", lambda: SimpleNamespace(llm_disabled=False))
    monkeypatch.setattr(storage_titles, "_dependency_identity", lambda: _dependency_identity())
    gate = asyncio.Event()
    active = 0
    peak = 0
    provider_calls = 0

    async def _fake_catalog_call(method, _params):
        if method == "storage.session.title.dependency.acquire.v2":
            return {"allowed": True, "incident_id": None}
        return {"changed": True}

    async def _model_call(**_kwargs):
        nonlocal active, peak, provider_calls
        active += 1
        provider_calls += 1
        peak = max(peak, active)
        try:
            await gate.wait()
            return "Bounded Provider Work"
        finally:
            active -= 1

    async def _close():
        return None

    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    monkeypatch.setattr(
        "zerg.models_config.get_llm_client_for_use_case",
        lambda _use_case: (SimpleNamespace(close=_close), "test-model", "test"),
    )
    monkeypatch.setattr("zerg.services.title_generator.generate_initial_session_title", _model_call)
    candidates = [
        _authorized_candidate(
            session_id=str(uuid4()), first_user_message=f"Title backlog item {index}", provider="opencode"
        )
        for index in range(8)
    ]

    first_wave = [storage_titles.schedule_storage_session_title(candidate) for candidate in candidates]
    assert first_wave == [True, True, True, True, False, False, False, False]
    assert storage_titles.schedule_storage_session_title(candidates[0]) is False
    for _ in range(100):
        if active == storage_titles.STORAGE_TITLE_MAX_CONCURRENCY:
            break
        await asyncio.sleep(0.01)
    assert active == storage_titles.STORAGE_TITLE_MAX_CONCURRENCY
    first_tasks = list(storage_titles._scheduled_tasks.values())
    gate.set()
    await asyncio.gather(*first_tasks)

    assert [storage_titles.schedule_storage_session_title(candidate) for candidate in candidates[4:]] == [True] * 4
    await asyncio.gather(*list(storage_titles._scheduled_tasks.values()))
    assert storage_titles._scheduled_tasks == {}
    assert provider_calls == 8
    assert peak == storage_titles.STORAGE_TITLE_MAX_CONCURRENCY


@pytest.mark.asyncio
async def test_worker_shutdown_releases_capacity_even_when_client_close_fails(monkeypatch):
    import zerg.services.storage_session_titles as storage_titles

    await storage_titles.stop_storage_title_workers()
    slots = asyncio.Semaphore(storage_titles.STORAGE_TITLE_MAX_CONCURRENCY)
    monkeypatch.setattr(storage_titles, "_model_slots", slots)
    monkeypatch.setattr(storage_titles, "STORAGE_TITLE_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(storage_titles, "get_settings", lambda: SimpleNamespace(llm_disabled=False))
    monkeypatch.setattr(storage_titles, "_dependency_identity", lambda: _dependency_identity())
    entered = asyncio.Event()

    async def _fake_catalog_call(method, _params):
        if method == "storage.session.title.dependency.acquire.v2":
            return {"allowed": True, "incident_id": None}
        return {"changed": True}

    async def _blocked_model(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def _hanging_close():
        await asyncio.Event().wait()

    monkeypatch.setattr(storage_titles, "_catalog_call", _fake_catalog_call)
    monkeypatch.setattr(
        "zerg.models_config.get_llm_client_for_use_case",
        lambda _use_case: (SimpleNamespace(close=_hanging_close), "test-model", "test"),
    )
    monkeypatch.setattr("zerg.services.title_generator.generate_initial_session_title", _blocked_model)
    assert storage_titles.schedule_storage_session_title(
        _authorized_candidate(
            session_id=str(uuid4()), first_user_message="Cancel this title worker", provider="opencode"
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    await storage_titles.stop_storage_title_workers()

    assert storage_titles._scheduled_tasks == {}
    assert slots._value == storage_titles.STORAGE_TITLE_MAX_CONCURRENCY


def _insert_session(engine, *, session_id, first_message, environment="local", provider="opencode", project="zerg"):
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
                project=project,
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


def test_row_specific_title_failure_is_bounded_then_persists_terminal(tmp_path):
    engine = _build_engine(tmp_path)
    session_id = uuid4()
    first_message = "fix the refresh token bug"
    _insert_session(engine, session_id=session_id, first_message=first_message)
    store = CatalogStore(engine)
    now = datetime.now(UTC)

    for attempt in range(1, MAX_TITLE_ATTEMPTS + 1):
        result = store.fail_storage_title(
            session_id=session_id,
            reason="invalid_title_payload",
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

    sixth = store.fail_storage_title(session_id=session_id, reason="invalid_title_payload", failed_at=now)
    assert sixth["changed"] is False  # terminal state: no further attempt counting


def test_terminal_state_survives_restart(tmp_path):
    db_path = tmp_path / "catalog.db"
    engine = create_catalog_engine(db_path)
    initialize_catalog_schema(engine)
    session_id = uuid4()
    _insert_session(engine, session_id=session_id, first_message="keep returning malformed output")
    store = CatalogStore(engine)
    now = datetime.now(UTC)
    for _ in range(MAX_TITLE_ATTEMPTS):
        store.fail_storage_title(session_id=session_id, reason="invalid_title_payload", failed_at=now)
    engine.dispose()

    fresh = create_catalog_engine(db_path)
    initialize_catalog_schema(fresh)
    assert str(session_id) not in _candidate_ids(fresh)


def test_empty_model_response_remains_a_slow_retry_obligation_after_budget(tmp_path):
    engine = _build_engine(tmp_path)
    session_id = uuid4()
    _insert_session(engine, session_id=session_id, first_message="recover from an empty model response")
    store = CatalogStore(engine)
    now = datetime.now(UTC)

    result = None
    expected_attempts = [1, 2, 3, 4, 5, 5]
    for expected_attempt in expected_attempts:
        result = store.fail_storage_title(session_id=session_id, reason="empty_model_response", failed_at=now)
        assert result["changed"] is True
        assert result["attempt_count"] == expected_attempt
        assert result["terminal"] is False

    assert result is not None
    assert datetime.fromisoformat(result["retry_at"]) == now + TITLE_ROW_TRANSIENT_RETRY_DELAY
    with engine.begin() as connection:
        connection.execute(
            update(StorageSession.__table__)
            .where(StorageSession.__table__.c.session_id == str(session_id))
            .values(title_retry_at=now - timedelta(seconds=1))
        )
    assert str(session_id) in _candidate_ids(engine)

    health = store.read_storage_title_dependency_health()
    assert health["terminal_sessions"] == 0
    assert health["pending_sessions"] == 1

    terminal = store.fail_storage_title(session_id=session_id, reason="invalid_title_payload", failed_at=now)
    assert terminal["terminal"] is True
    assert terminal["attempt_count"] == MAX_TITLE_ATTEMPTS
    assert str(session_id) not in _candidate_ids(engine)


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
        store.fail_storage_title(session_id=burnt_id, reason="invalid_title_payload", failed_at=now + timedelta(seconds=1))

    candidates = _candidate_ids(engine)
    assert str(normal_id) in candidates
    assert str(seed_id) not in candidates
    assert str(burnt_id) not in candidates


def test_typed_factory_title_assurance_is_the_only_factory_machine_title_obligation(tmp_path):
    engine = _build_engine(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    assurance_id = uuid4()
    ordinary_factory_id = uuid4()
    common = {
        "tenant_id": "tenant-a",
        "owner_id": "42",
        "provider": "claude",
        "environment": "local",
        "machine_id": "provider-factory-resume",
        "project": "longhouse-title-assurance",
        "cwd": "/factory/title-assurance",
        "started_at": now,
        "last_activity_at": now,
        "user_messages": 1,
        "semantic_projection_version": 1,
        "first_user_message_preview": "Verify native Claude title projection",
        "origin_kind": "console",
        "hidden_from_default_timeline": 1,
        "launch_actor": "automation",
        "commit_seq": 1,
        "created_at": now,
        "updated_at": now,
    }
    with engine.begin() as connection:
        connection.execute(
            StorageSession.__table__.insert(),
            [
                {**common, "session_id": str(assurance_id), "launch_surface": "factory_assurance"},
                {**common, "session_id": str(ordinary_factory_id), "launch_surface": "test"},
            ],
        )

    candidates = _candidate_ids(engine)
    assert str(assurance_id) in candidates
    assert str(ordinary_factory_id) not in candidates
    health = CatalogStore(engine).read_storage_title_dependency_health()
    assert health["pending_sessions"] == 1


def test_candidate_listing_drains_oldest_due_obligation_before_new_arrivals(tmp_path):
    engine = _build_engine(tmp_path)
    old_id = uuid4()
    new_id = uuid4()
    _insert_session(engine, session_id=old_id, first_message="old durable title debt")
    _insert_session(engine, session_id=new_id, first_message="new arrival")
    old = datetime.now(UTC) - timedelta(hours=1)
    with engine.begin() as connection:
        connection.execute(
            update(StorageSession.__table__)
            .where(StorageSession.__table__.c.session_id == str(old_id))
            .values(created_at=old, last_activity_at=old, title_retry_at=old)
        )

    result = CatalogStore(engine).list_storage_title_candidates(limit=1)

    assert [row["session_id"] for row in result["sessions"]] == [str(old_id)]


def test_old_seed_markers_cannot_consume_bounded_candidate_page(tmp_path):
    engine = _build_engine(tmp_path)
    seed_ids = [uuid4() for _ in range(10)]
    for index, session_id in enumerate(seed_ids):
        _insert_session(
            engine,
            session_id=session_id,
            first_message=f"LONGHOUSE_CODEX_RESUME_SEED_{index:08x}",
        )
    normal_id = uuid4()
    _insert_session(engine, session_id=normal_id, first_message="real user title obligation")
    old = datetime.now(UTC) - timedelta(hours=1)
    with engine.begin() as connection:
        connection.execute(
            update(StorageSession.__table__)
            .where(StorageSession.__table__.c.session_id.in_([str(session_id) for session_id in seed_ids]))
            .values(created_at=old, last_activity_at=old)
        )

    result = CatalogStore(engine).list_storage_title_candidates(limit=1)

    assert [row["session_id"] for row in result["sessions"]] == [str(normal_id)]


def test_synthetic_benchmark_project_is_not_a_title_obligation(tmp_path):
    engine = _build_engine(tmp_path)
    synthetic_id = uuid4()
    normal_id = uuid4()
    _insert_session(
        engine,
        session_id=synthetic_id,
        first_message="synthetic bench file=0 event=0 payload",
        project="longhouse-bench",
    )
    _insert_session(engine, session_id=normal_id, first_message="real human title request")

    candidates = _candidate_ids(engine)

    assert str(synthetic_id) not in candidates
    assert str(normal_id) in candidates


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
        return (
            connection.execute(select(StorageSession.__table__).where(StorageSession.__table__.c.session_id == str(session_id)))
            .mappings()
            .one()
        )


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
    assert (
        store.acquire_storage_title_dependency(
            session_id=first_id,
            probe_token=first_token,
            observed_at=now,
            lease_seconds=60,
            **identity,
        )["allowed"]
        is True
    )
    assert (
        store.acquire_storage_title_dependency(
            session_id=second_id,
            probe_token=second_token,
            observed_at=now,
            lease_seconds=60,
            **identity,
        )["allowed"]
        is True
    )

    first = store.fail_storage_title_dependency(
        session_id=first_id,
        probe_token=first_token,
        failure_class="authentication",
        reason="Error code: 401 - invalid API key",
        failed_at=now,
        **identity,
    )
    second = store.fail_storage_title_dependency(
        session_id=second_id,
        probe_token=second_token,
        failure_class="authentication",
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
    assert datetime.fromisoformat(first["next_probe_at"]) == now + timedelta(seconds=60)
    health = store.read_storage_title_dependency_health()
    assert health["open_dependencies"] == 1
    assert health["blocked_sessions"] == 2


def test_dependency_timeout_coalesces_without_spending_row_attempts(tmp_path):
    engine = _build_engine(tmp_path)
    first_id = uuid4()
    second_id = uuid4()
    _insert_session(engine, session_id=first_id, first_message="first timeout obligation")
    _insert_session(engine, session_id=second_id, first_message="second timeout obligation")
    store = CatalogStore(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    identity = _dependency_identity()
    store.reconcile_storage_title_dependency(**identity, observed_at=now)

    first = store.fail_storage_title_dependency(
        session_id=first_id,
        probe_token=uuid4(),
        failure_class="availability",
        reason="APITimeoutError: request timed out",
        failed_at=now,
        **identity,
    )
    second = store.fail_storage_title_dependency(
        session_id=second_id,
        probe_token=uuid4(),
        failure_class="availability",
        reason="503 temporarily unavailable",
        failed_at=now,
        **identity,
    )

    assert first["incident_id"] == second["incident_id"]
    assert first["new_incident"] is True
    assert second["new_incident"] is False
    assert _title_row(engine, first_id)["title_attempt_count"] == 0
    assert _title_row(engine, second_id)["title_attempt_count"] == 0
    assert _title_row(engine, first_id)["title_dependency_incident_id"] == first["incident_id"]
    assert _title_row(engine, second_id)["title_dependency_incident_id"] == first["incident_id"]
    assert datetime.fromisoformat(first["next_probe_at"]) == now + timedelta(seconds=5)


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
        failure_class="authentication",
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
        failure_class="authentication",
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
    _insert_session(engine, session_id=unrelated_id, first_message="row-specific debt")
    store = CatalogStore(engine)
    now = (datetime.now(UTC) - timedelta(seconds=5)).replace(microsecond=0)
    for _ in range(MAX_TITLE_ATTEMPTS):
        store.fail_storage_title(session_id=auth_id, reason="AuthenticationError: 401", failed_at=now)
        store.fail_storage_title(session_id=unrelated_id, reason="invalid_title_payload", failed_at=now)

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
    assert unrelated_row["title_last_error"] == "invalid_title_payload"
    assert str(auth_id) in _candidate_ids(engine)
    assert str(unrelated_id) not in _candidate_ids(engine)


def test_one_time_legacy_repair_rearms_terminal_timeout_but_preserves_row_failure(tmp_path):
    engine = _build_engine(tmp_path)
    timeout_id = uuid4()
    malformed_id = uuid4()
    _insert_session(engine, session_id=timeout_id, first_message="legacy timeout debt")
    _insert_session(engine, session_id=malformed_id, first_message="legacy malformed debt")
    store = CatalogStore(engine)
    now = (datetime.now(UTC) - timedelta(minutes=10)).replace(microsecond=0)
    for _ in range(MAX_TITLE_ATTEMPTS):
        store.fail_storage_title(session_id=timeout_id, reason="TimeoutError", failed_at=now)
        store.fail_storage_title(session_id=malformed_id, reason="invalid_title_payload", failed_at=now)

    identity_a = _dependency_identity("a" * 64)
    adopted = store.reconcile_storage_title_dependency(**identity_a, observed_at=now + timedelta(seconds=1))
    incident_id = adopted["dependency"]["incident_id"]
    assert adopted["adopted_sessions"] == 1
    assert adopted["dependency"]["failure_class"] == "availability"
    assert adopted["dependency"]["legacy_repair_version"] == 2
    assert _title_row(engine, timeout_id)["title_dependency_incident_id"] == incident_id
    assert _title_row(engine, malformed_id)["title_dependency_incident_id"] is None

    identity_b = _dependency_identity("b" * 64)
    store.reconcile_storage_title_dependency(**identity_b, observed_at=now + timedelta(seconds=2))
    token = uuid4()
    assert (
        store.acquire_storage_title_dependency(
            session_id=timeout_id,
            probe_token=token,
            observed_at=now + timedelta(seconds=2),
            lease_seconds=60,
            **identity_b,
        )["allowed"]
        is True
    )
    recovered = store.recover_storage_title_dependency(
        incident_id=UUID(incident_id),
        probe_token=token,
        recovered_at=now + timedelta(seconds=3),
        **identity_b,
    )

    assert recovered["rearmed_sessions"] == 1
    assert _title_row(engine, timeout_id)["title_attempt_count"] == 0
    assert _title_row(engine, malformed_id)["title_attempt_count"] == MAX_TITLE_ATTEMPTS
    assert str(timeout_id) in _candidate_ids(engine)
    assert str(malformed_id) not in _candidate_ids(engine)


def test_title_health_degrades_for_aged_backlog_and_terminal_row_debt(tmp_path):
    engine = _build_engine(tmp_path)
    overdue_id = uuid4()
    terminal_id = uuid4()
    _insert_session(engine, session_id=overdue_id, first_message="old pending title")
    _insert_session(engine, session_id=terminal_id, first_message="terminal malformed title")
    old = datetime.now(UTC) - timedelta(minutes=10)
    with engine.begin() as connection:
        connection.execute(
            update(StorageSession.__table__)
            .where(StorageSession.__table__.c.session_id == str(overdue_id))
            .values(last_activity_at=old, title_retry_at=old)
        )
    store = CatalogStore(engine)
    for _ in range(MAX_TITLE_ATTEMPTS):
        store.fail_storage_title(session_id=terminal_id, reason="invalid_title_payload", failed_at=old)

    health = store.read_storage_title_dependency_health()

    assert health["status"] == "degraded"
    assert health["overdue_sessions"] == 1
    assert health["oldest_overdue_age_seconds"] >= 9 * 60
    assert health["terminal_sessions"] == 1
    assert health["terminal_shared_failure_sessions"] == 0
