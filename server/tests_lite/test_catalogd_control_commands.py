from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveMachineControlOperation
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.live_control_catalog import get_live_control_grant


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-control-command-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_control_grant(engine, *, bind_adapter_identity: bool = True, provider: str = "codex"):
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider=provider,
                environment="production",
                device_id="cinder",
                started_at=now,
                primary_thread_id=str(thread_id),
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveSessionThread(
                id=str(thread_id),
                session_id=str(session_id),
                provider=provider,
                branch_kind="root",
                is_primary=1,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveSessionRun(
                id=str(run_id),
                thread_id=str(thread_id),
                provider=provider,
                host_id="cinder",
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
        adapter_connection_id = str(uuid4()) if bind_adapter_identity else None
        lease_generation = str(uuid4()) if bind_adapter_identity else None
        connection = LiveSessionConnection(
            run_id=str(run_id),
            adapter_connection_id=adapter_connection_id,
            lease_generation=lease_generation,
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            device_id="cinder",
            can_send_input=1,
            can_interrupt=1,
            can_terminate=1,
            acquired_at=now,
            last_health_at=now,
        )
        db.add(connection)
        db.commit()
        return session_id, run_id, connection.id, adapter_connection_id, lease_generation


def _reduce_control_fact(
    engine,
    *,
    session_id,
    run_id,
    adapter_connection_id,
    lease_generation,
    grants=("interrupt", "send_input", "terminate"),
    state="attached",
    observed_at=None,
    lease_ttl_ms=900_000,
    provider="codex",
):
    observed_at = observed_at or datetime.now(UTC).replace(microsecond=0)
    value = {
        "authority_class": "provider_control",
        "provider": provider,
        "session_id": str(session_id),
        "run_id": str(run_id),
        "connection_id": str(adapter_connection_id),
        "lease_generation": str(lease_generation),
        "granted_operations": list(grants),
        "state": state,
        "lease_ttl_ms": lease_ttl_ms,
        "source": f"{provider}_control_scan",
        "observed_at": observed_at.isoformat(),
    }
    fact = ReducerFact(
        family="control",
        subject_key=f"connection:{adapter_connection_id}:{lease_generation}",
        source=f"{provider}_control_scan",
        source_epoch=str(lease_generation),
        source_seq=None,
        dedupe_key=canonical_evidence_hash({**value, "dedupe": observed_at.isoformat()}),
        evidence_hash=canonical_evidence_hash(value),
        value=value,
        observed_at=observed_at,
        session_id=str(session_id),
    )
    with engine.begin() as connection:
        reduce_fact_batch(connection, [fact], received_at=observed_at)


def _prepare_params(session_id, *, operation_id=None, command_id=None, provider="codex"):
    operation_id = operation_id or str(uuid4())
    return {
        "operation_id": operation_id,
        "owner_id": 7,
        "session_id": str(session_id),
        "device_id": "cinder",
        "provider": provider,
        "command_type": "session.send_text",
        "command_id": command_id or f"managed-control:{session_id}:session.send_text:{operation_id}",
        "capability": "send",
        "request_payload": {"session_id": str(session_id), "payload": {"text": "continue"}},
        "timeout_secs": 15,
    }


@pytest.mark.asyncio
async def test_catalogd_prepares_and_finishes_control_command_atomically(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, connection_id, adapter_connection_id, lease_generation = _seed_control_grant(engine)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    operation_id = str(uuid4())
    command_id = f"managed-control:{session_id}:session.send_text:req-1"
    params = {
        "operation_id": operation_id,
        "owner_id": 7,
        "session_id": str(session_id),
        "device_id": "cinder",
        "provider": "codex",
        "command_type": "session.send_text",
        "command_id": command_id,
        "capability": "send",
        "request_payload": {"session_id": str(session_id), "payload": {"text": "continue"}},
        "timeout_secs": 15,
    }
    try:
        prepared = await client.call("control.command.prepare.v2", params)
        assert prepared["allowed"] is True
        assert prepared["operation_id"] == operation_id
        assert prepared["grant"]["connection_id"] == adapter_connection_id
        assert prepared["grant"]["catalog_connection_id"] == connection_id
        assert prepared["grant"]["run_id"] == str(run_id)
        assert prepared["grant"]["lease_generation"] == lease_generation
        assert prepared["grant"]["identity_source"] == "adapter_bound"
        replay = await client.call("control.command.prepare.v2", params)
        assert replay["allowed"] is True
        assert replay["exact_replay"] is True
        assert replay["operation_id"] == operation_id
        assert replay["grant"] == prepared["grant"]
        finished = await client.call(
            "control.operation.finish.v2",
            {
                "operation_id": operation_id,
                "status": "succeeded",
                "result": {"exit_code": 0, "stdout": "accepted", "stderr": ""},
                "error": None,
            },
        )
        assert finished["found"] is True
        assert finished["changed"] is True
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        operation = db.get(LiveMachineControlOperation, operation_id)
        assert operation.status == "succeeded"
        assert json.loads(operation.request_json)["longhouse_control_grant"]["run_id"] == str(run_id)
        assert json.loads(operation.result_json)["stdout"] == "accepted"
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_control_prepare_preserves_legacy_identity_for_unbound_connection(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, connection_id, _adapter_id, _generation = _seed_control_grant(
        engine,
        bind_adapter_identity=False,
    )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        prepared = await client.call(
            "control.command.prepare.v2",
            {
                "operation_id": str(uuid4()),
                "owner_id": 7,
                "session_id": str(session_id),
                "device_id": "cinder",
                "provider": "codex",
                "command_type": "session.send_text",
                "command_id": f"managed-control:{session_id}:session.send_text:legacy",
                "capability": "send",
                "request_payload": {},
                "timeout_secs": 15,
            },
        )
    finally:
        await client.close()
        await daemon.close()

    assert prepared["allowed"] is True
    assert prepared["grant"]["connection_id"] == connection_id
    assert prepared["grant"]["catalog_connection_id"] == connection_id
    assert prepared["grant"]["run_id"] == str(run_id)
    assert prepared["grant"]["lease_generation"].startswith(f"{connection_id}:")
    assert prepared["grant"]["identity_source"] == "legacy_synthetic"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_canonical_control_prepare_allows_only_bound_matching_grant(monkeypatch, daemon_paths, provider):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, connection_id, adapter_id, generation = _seed_control_grant(engine, provider=provider)
    _reduce_control_fact(
        engine,
        session_id=session_id,
        run_id=run_id,
        adapter_connection_id=adapter_id,
        lease_generation=generation,
        provider=provider,
    )
    with Session(engine) as db:
        connection = db.get(LiveSessionConnection, connection_id)
        connection.can_send_input = 0
        db.commit()
    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_COMMAND_AUTH", "canonical")
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")

    prepared = CatalogStore(engine).prepare_control_command(**_prepare_params(session_id, provider=provider))

    assert prepared["allowed"] is True
    assert prepared["grant"] == {
        "connection_id": adapter_id,
        "catalog_connection_id": connection_id,
        "run_id": str(run_id),
        "lease_generation": generation,
        "identity_source": "adapter_bound",
    }
    engine.dispose()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_canonical_control_prepare_ignores_transcript_ended_at(monkeypatch, daemon_paths, provider):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, _connection_id, adapter_id, generation = _seed_control_grant(engine, provider=provider)
    _reduce_control_fact(
        engine,
        session_id=session_id,
        run_id=run_id,
        adapter_connection_id=adapter_id,
        lease_generation=generation,
        provider=provider,
    )
    with Session(engine) as db:
        session = db.get(LiveSessionCatalog, str(session_id))
        session.ended_at = datetime.now(UTC)
        db.commit()
    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_COMMAND_AUTH", "canonical")
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")

    prepared = CatalogStore(engine).prepare_control_command(**_prepare_params(session_id, provider=provider))

    assert prepared["allowed"] is True
    engine.dispose()


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("terminal_axis", ["session", "run"])
def test_control_prepare_revalidates_explicit_session_and_latest_run_terminal(
    monkeypatch,
    daemon_paths,
    canonical,
    terminal_axis,
):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, _connection_id, adapter_id, generation = _seed_control_grant(engine)
    if canonical:
        _reduce_control_fact(
            engine,
            session_id=session_id,
            run_id=run_id,
            adapter_connection_id=adapter_id,
            lease_generation=generation,
        )
        monkeypatch.setenv("LONGHOUSE_SESSION_STATE_COMMAND_AUTH", "canonical")
        monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    with Session(engine) as db:
        if terminal_axis == "session":
            session = db.get(LiveSessionCatalog, str(session_id))
            session.closed_at = datetime.now(UTC)
            session.close_reason = "user_closed"
        else:
            run = db.get(LiveSessionRun, str(run_id))
            run.ended_at = datetime.now(UTC)
            run.exit_status = "completed"
        db.commit()

    prepared = CatalogStore(engine).prepare_control_command(**_prepare_params(session_id))

    assert prepared["allowed"] is False
    assert prepared["reason"] == ("session_closed" if terminal_axis == "session" else "run_ended")
    engine.dispose()


def test_canonical_control_prepare_rejects_unbound_and_ungranted(monkeypatch, daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    unbound_session, *_rest = _seed_control_grant(engine, bind_adapter_identity=False)
    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_COMMAND_AUTH", "canonical")
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")

    unbound = CatalogStore(engine).prepare_control_command(**_prepare_params(unbound_session))

    assert unbound["allowed"] is False
    assert unbound["reason"] == "identity_unbound"

    session_id, run_id, _connection_id, adapter_id, generation = _seed_control_grant(engine)
    _reduce_control_fact(
        engine,
        session_id=session_id,
        run_id=run_id,
        adapter_connection_id=adapter_id,
        lease_generation=generation,
        grants=(),
    )

    ungranted = CatalogStore(engine).prepare_control_command(**_prepare_params(session_id))

    assert ungranted["allowed"] is False
    assert ungranted["reason"] == "not_granted"
    engine.dispose()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_canonical_exact_replay_revalidates_current_grant(monkeypatch, daemon_paths, provider):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, _connection_id, adapter_id, generation = _seed_control_grant(engine, provider=provider)
    observed_at = datetime.now(UTC).replace(microsecond=0)
    _reduce_control_fact(
        engine,
        session_id=session_id,
        run_id=run_id,
        adapter_connection_id=adapter_id,
        lease_generation=generation,
        observed_at=observed_at,
        provider=provider,
    )
    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_COMMAND_AUTH", "canonical")
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    params = _prepare_params(session_id, provider=provider)
    store = CatalogStore(engine)
    prepared = store.prepare_control_command(**params)
    assert prepared["allowed"] is True

    _reduce_control_fact(
        engine,
        session_id=session_id,
        run_id=run_id,
        adapter_connection_id=adapter_id,
        lease_generation=generation,
        grants=(),
        observed_at=observed_at.replace(microsecond=1),
        provider=provider,
    )
    replay = store.prepare_control_command(**params)

    assert replay["allowed"] is False
    assert replay["exact_replay"] is False
    assert replay["reason"] == "not_granted"
    engine.dispose()


def test_canonical_control_prepare_requires_reducer_ingest(monkeypatch, daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, _connection_id, adapter_id, generation = _seed_control_grant(engine)
    _reduce_control_fact(
        engine,
        session_id=session_id,
        run_id=run_id,
        adapter_connection_id=adapter_id,
        lease_generation=generation,
    )
    monkeypatch.setenv("LONGHOUSE_SESSION_STATE_COMMAND_AUTH", "canonical")

    prepared = CatalogStore(engine).prepare_control_command(**_prepare_params(session_id))

    assert prepared["allowed"] is False
    assert prepared["reason"] == "canonical_ingest_disabled"
    engine.dispose()


def test_finished_control_operation_cannot_be_rearmed(monkeypatch, daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, _run_id, _connection_id, _adapter_id, _generation = _seed_control_grant(engine)
    params = _prepare_params(session_id)
    store = CatalogStore(engine)
    prepared = store.prepare_control_command(**params)
    assert prepared["allowed"] is True
    store.finish_control_operation(
        operation_id=params["operation_id"],
        status="succeeded",
        result={"ok": True},
        error=None,
    )

    replay = store.prepare_control_command(**params)

    assert replay["allowed"] is False
    assert replay["reason"] == "operation_finished"
    engine.dispose()


@pytest.mark.parametrize(
    ("adapter_connection_id", "lease_generation"),
    [(str(uuid4()), None), (None, str(uuid4()))],
)
def test_control_grant_fails_closed_on_partial_adapter_identity(
    daemon_paths,
    adapter_connection_id,
    lease_generation,
):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, _run_id, connection_id, _adapter_id, _generation = _seed_control_grant(
        engine,
        bind_adapter_identity=False,
    )
    with Session(engine) as db:
        connection = db.get(LiveSessionConnection, connection_id)
        connection.adapter_connection_id = adapter_connection_id
        connection.lease_generation = lease_generation
        db.commit()

        assert get_live_control_grant(db, session_id=session_id, capability="send") is None
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_control_prepare_fails_closed_without_current_grant(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call(
            "control.command.prepare.v2",
            {
                "operation_id": str(uuid4()),
                "owner_id": 7,
                "session_id": str(uuid4()),
                "device_id": "cinder",
                "provider": "codex",
                "command_type": "session.send_text",
                "command_id": f"managed-control:{uuid4()}:session.send_text:req-2",
                "capability": "send",
                "request_payload": {},
                "timeout_secs": 15,
            },
        )
        assert result["allowed"] is False
        assert result["reason"] == "control_unavailable"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_owns_provider_live_operation_lifecycle(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    operation_id = str(uuid4())
    params = {
        "operation_id": operation_id,
        "owner_id": 7,
        "device_id": "cinder",
        "provider": "claude",
        "command_type": "provider.live_proof",
        "command_id": f"machine-op:{operation_id}",
        "request_payload": {"provider": "claude", "publish": True},
        "timeout_secs": 135,
    }
    try:
        prepared = await client.call("machine.operation.prepare.v2", params)
        assert prepared["created"] is True
        assert prepared["operation"]["status"] == "running"
        replay = await client.call("machine.operation.prepare.v2", params)
        assert replay["exact_replay"] is True
        assert replay["commit_seq"] == prepared["commit_seq"]

        conflicting = {**params, "operation_id": str(uuid4())}
        conflicting["command_id"] = f"machine-op:{conflicting['operation_id']}"
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("machine.operation.prepare.v2", conflicting)
        assert exc_info.value.code == "conflict"

        running = await client.call(
            "machine.operation.read.v2",
            {"owner_id": 7, "operation_id": operation_id},
        )
        assert running["operation"]["request"]["provider"] == "claude"
        await client.call(
            "control.operation.finish.v2",
            {
                "operation_id": operation_id,
                "status": "failed",
                "result": None,
                "error": {"code": "proof_failed", "message": "no proof"},
            },
        )
        failed = await client.call(
            "machine.operation.read.v2",
            {"owner_id": 7, "operation_id": operation_id},
        )
        assert failed["operation"]["status"] == "failed"
        assert failed["operation"]["error"]["code"] == "proof_failed"
    finally:
        await client.close()
        await daemon.close()
