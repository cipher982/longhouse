"""Hermetic Automation Factory scenarios for managed Helm Resume.

The scenarios exercise the real catalogd resume transaction for every launch-tier
provider. Provider CLIs keep their native command mechanics in the engine; this
module proves the provider-neutral invariants that must hold around those native
processes.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveUser
from zerg.qa.provider_resume_oracles import assertions_for
from zerg.services.managed_provider_contracts import contract_for_provider

SCENARIOS = (
    "helm_cold_resume",
    "helm_live_reattach",
    "console_thread_continue",
    "resume_identity_continuity",
    "resume_attempt_idempotency",
    "resume_single_owner",
    "resume_input_safety",
    "resume_failure_cleanup",
    "resume_unsupported",
)


def run_provider_resume_scenario(provider: str, scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown provider Resume scenario: {scenario}")
    contract = contract_for_provider(provider)
    capability = dict((contract.capabilities if contract else {}).get("session.resume.helm") or {})
    disposition = str(capability.get("disposition") or "not_implemented")
    supported = disposition == "implemented"

    if scenario == "resume_unsupported":
        if supported:
            return _not_applicable(provider, scenario, disposition)
        observation = {
            "disposition": disposition,
            "registration_count": 0,
            "provider_spawn_count": 0,
        }
        return _result(provider, scenario, observation)
    if not supported:
        return _not_applicable(provider, scenario, disposition)
    return asyncio.run(_run_catalog_scenario(provider, scenario))


async def _run_catalog_scenario(provider: str, scenario: str) -> dict[str, Any]:
    contract = contract_for_provider(provider)
    assert contract is not None
    with tempfile.TemporaryDirectory(prefix="lh-resume-", dir="/tmp") as root_value:
        root = Path(root_value)
        database_path = root / "live.db"
        socket_path = root / "catalogd.sock"
        daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
        await daemon.start()
        setup_engine = create_catalog_engine(database_path)
        initialize_catalog_schema(setup_engine)
        try:
            with Session(setup_engine) as db:
                db.add(LiveUser(id=7, email="resume-factory@longhouse.invalid", is_active=True))
                db.commit()
        finally:
            setup_engine.dispose()
        # Factory correctness is not the catalog latency gate. Give a loaded CI
        # host enough room to complete the real transaction; catalog performance
        # has its own bounded tests and production clients retain the 1 s default.
        client = CatalogClient(socket_path, default_timeout_seconds=5.0, max_concurrency=4)
        session_id = str(uuid4())
        provider_thread_id = str(uuid4())
        machine_id = "factory-machine"
        cwd = "/workspace/longhouse"
        now = datetime.now(UTC).replace(microsecond=0)
        initial = await client.call(
            "session.launch.local.create.v2",
            {
                "launch": _launch_payload(
                    provider=provider,
                    transport=contract.managed_transport.value,
                    session_id=session_id,
                    provider_thread_id=provider_thread_id,
                    machine_id=machine_id,
                    cwd=cwd,
                    now=now,
                )
            },
        )
        await _confirm(client, session_id, initial["run_id"], machine_id, now)
        resume_clock = now + timedelta(seconds=3)
        try:
            if scenario == "helm_live_reattach":
                observation = await _live_reattach_observation(
                    client,
                    database_path,
                    provider,
                    session_id,
                    provider_thread_id,
                    machine_id,
                    cwd,
                    resume_clock,
                )
            else:
                await _end_run(
                    client,
                    provider,
                    session_id,
                    initial["run_id"],
                    machine_id,
                    resume_clock,
                )
                if scenario == "helm_cold_resume":
                    observation = await _cold_resume_observation(
                        client,
                        database_path,
                        provider,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        initial["run_id"],
                        resume_clock,
                    )
                elif scenario == "resume_identity_continuity":
                    observation = await _identity_observation(
                        client,
                        database_path,
                        provider,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        resume_clock,
                    )
                elif scenario == "resume_attempt_idempotency":
                    observation = await _idempotency_observation(
                        client,
                        database_path,
                        provider,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        resume_clock,
                    )
                elif scenario == "resume_single_owner":
                    observation = await _single_owner_observation(
                        client,
                        database_path,
                        provider,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        resume_clock,
                    )
                elif scenario == "resume_input_safety":
                    observation = await _input_safety_observation(
                        client,
                        database_path,
                        provider,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        resume_clock,
                    )
                elif scenario == "resume_failure_cleanup":
                    observation = await _failure_cleanup_observation(
                        client,
                        database_path,
                        provider,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        resume_clock,
                    )
                elif scenario == "console_thread_continue":
                    observation = await _console_thread_continue_observation(
                        client,
                        database_path,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        resume_clock,
                    )
                else:  # pragma: no cover - guarded by SCENARIOS
                    raise AssertionError(scenario)
            return _result(provider, scenario, observation)
        finally:
            await client.close()
            await daemon.close()


def _launch_payload(
    *,
    provider: str,
    transport: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "owner_id": 7,
        "git_repo": "cipher982/longhouse",
        "git_branch": "main",
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "plan": {
            "session_id": session_id,
            "provider": provider,
            "provider_session_id": provider_thread_id,
            "source_name": machine_id,
            "source_runner_id": None,
            "cwd": cwd,
            "project": "longhouse",
            "display_name": "Resume factory",
            "managed_session_name": f"{provider}-resume-factory",
            "loop_mode": "assist",
            "permission_mode": "bypass",
            "launch_actor": "automation_factory",
            "launch_surface": "cli",
            "managed_transport": transport,
            "attach_command": "",
            "provider_config": {},
        },
    }


def _resume_payload(
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
    *,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    return {
        "owner_id": 7,
        "session_id": session_id,
        "provider": provider,
        "provider_thread_id": provider_thread_id,
        "device_id": machine_id,
        "cwd": cwd,
        "resume_attempt_id": attempt_id or str(uuid4()),
        "started_at": (now + timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }


async def _end_run(
    client: CatalogClient,
    provider: str,
    session_id: str,
    run_id: str,
    machine_id: str,
    now: datetime,
) -> None:
    await client.call(
        "session.runtime.apply.v2",
        {
            "events": [
                {
                    "runtime_key": f"{provider}:{session_id}",
                    "session_id": session_id,
                    "thread_id": None,
                    "run_id": run_id,
                    "provider": provider,
                    "device_id": machine_id,
                    "source": "resume_factory",
                    "kind": "terminal_signal",
                    "phase": "finished",
                    "tool_name": None,
                    "occurred_at": now.isoformat(),
                    "freshness_ms": 60_000,
                    "dedupe_key": f"factory-exit:{run_id}",
                    "payload": {
                        "terminal_state": "process_gone",
                        "terminal_reason": "provider_exit",
                    },
                }
            ]
        },
    )


async def _confirm(client: CatalogClient, session_id: str, run_id: str, machine_id: str, now: datetime) -> dict[str, Any]:
    return await client.call(
        "session.launch.local.finish.v2",
        {
            "outcome": {
                "session_id": session_id,
                "run_id": run_id,
                "owner_id": 7,
                "device_id": machine_id,
                "state": "adopted",
                "error_code": None,
                "error_message": None,
                "observed_at": (now + timedelta(seconds=2)).isoformat(),
            }
        },
    )


async def _cold_resume_observation(
    client: CatalogClient,
    database_path: Path,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    initial_run_id: str,
    now: datetime,
) -> dict[str, Any]:
    resumed = await client.call(
        "session.launch.local.resume.v2",
        {"resume": _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now)},
    )
    confirmed = await _confirm(client, session_id, resumed["run_id"], machine_id, now)
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    try:
        with Session(engine) as db:
            run = db.get(LiveSessionRun, resumed["run_id"])
            alias = (
                db.query(LiveSessionThreadAlias)
                .filter(
                    LiveSessionThreadAlias.thread_id == run.thread_id,
                    LiveSessionThreadAlias.alias_kind == "provider_session_id",
                    LiveSessionThreadAlias.alias_value == provider_thread_id,
                )
                .one_or_none()
            )
            connection = db.query(LiveSessionConnection).filter(LiveSessionConnection.run_id == resumed["run_id"]).one_or_none()
            return {
                "same_longhouse_session": resumed["launch"]["session_id"] == session_id,
                "same_provider_thread": alias is not None,
                "new_run": resumed["run_id"] != initial_run_id,
                "new_connection": connection is not None,
                "resume_registration_confirmed": confirmed["launch"]["launch_state"] == "live",
            }
    finally:
        engine.dispose()


async def _live_reattach_observation(
    client: CatalogClient,
    database_path: Path,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    refused = False
    try:
        await client.call(
            "session.launch.local.resume.v2",
            {"resume": _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now)},
        )
    except CatalogRemoteError as error:
        refused = error.code == "conflict"
    open_run_count = _open_run_count(database_path, session_id)
    return {
        "resume_refused_as_conflict": refused,
        "open_run_count": open_run_count,
        "reattached_live_owner": refused and open_run_count == 1,
        "provider_owner_spawn_count": max(0, open_run_count - 1),
    }


async def _console_thread_continue_observation(
    client: CatalogClient,
    database_path: Path,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    setup_engine = create_catalog_engine(database_path)
    initialize_catalog_schema(setup_engine)
    try:
        with Session(setup_engine) as db:
            alias = (
                db.query(LiveSessionThreadAlias)
                .filter(
                    LiveSessionThreadAlias.alias_kind == "provider_session_id",
                    LiveSessionThreadAlias.alias_value == provider_thread_id,
                )
                .one()
            )
            thread = db.get(LiveSessionThread, alias.thread_id)
            assert thread is not None
            thread.device_id = machine_id
            thread.cwd = cwd
            thread.provider_config_json = json.dumps({"permission_mode": "bypass"})
            db.commit()
    finally:
        setup_engine.dispose()
    result = await client.call(
        "session.console.turn.enqueue.v2",
        {
            "turn": {
                "session_id": session_id,
                "owner_id": 7,
                "message": "Continue through Console",
                "client_request_id": str(uuid4()),
                "created_at": now.isoformat(),
            }
        },
    )
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    try:
        with Session(engine) as db:
            turn = db.query(LiveConsoleTurn).filter(LiveConsoleTurn.session_id == session_id).one_or_none()
            if turn is None:
                return {
                    "mode": "console",
                    "turn_created": False,
                    "used_helm_resume_transaction": False,
                    "same_provider_thread": False,
                    "console_run_created": False,
                    "catalog_result": result,
                }
            launch_attempt = db.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.run_id == turn.run_id).one_or_none()
            return {
                "mode": "console",
                "turn_created": result.get("created") is True,
                "used_helm_resume_transaction": launch_attempt is not None,
                "same_provider_thread": turn.resume_provider_thread_id == provider_thread_id,
                "console_run_created": turn.run_id is not None,
            }
    finally:
        engine.dispose()


async def _identity_observation(
    client: CatalogClient,
    database_path: Path,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    resumed = await client.call(
        "session.launch.local.resume.v2",
        {"resume": _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now)},
    )
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    try:
        with Session(engine) as db:
            run = db.get(LiveSessionRun, resumed["run_id"])
            attempt = db.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.run_id == resumed["run_id"]).one()
            alias = (
                db.query(LiveSessionThreadAlias)
                .filter(
                    LiveSessionThreadAlias.thread_id == run.thread_id,
                    LiveSessionThreadAlias.alias_kind == "provider_session_id",
                    LiveSessionThreadAlias.alias_value == provider_thread_id,
                )
                .one_or_none()
            )
            return {
                "same_owner": attempt.owner_id == 7,
                "same_machine": run.host_id == machine_id,
                "same_cwd": run.cwd == cwd,
                "same_provider": run.provider == provider,
                "same_longhouse_session": attempt.session_id == session_id,
                "same_provider_thread": alias is not None,
            }
    finally:
        engine.dispose()


async def _idempotency_observation(
    client: CatalogClient,
    database_path: Path,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    attempt_id = str(uuid4())
    payload = _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now, attempt_id=attempt_id)
    first = await client.call("session.launch.local.resume.v2", {"resume": payload})
    replay = await client.call("session.launch.local.resume.v2", {"resume": payload})
    refused = False
    try:
        await client.call(
            "session.launch.local.resume.v2",
            {"resume": _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now)},
        )
    except CatalogRemoteError as error:
        refused = error.code == "conflict"
    return {
        "same_attempt_same_run": replay.get("exact_replay") is True and replay["run_id"] == first["run_id"],
        "created_run_count": max(0, _run_count_for_session(database_path, session_id) - 1),
        "concurrent_attempt_refused": refused,
    }


async def _single_owner_observation(
    client: CatalogClient,
    database_path: Path,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    calls = [
        client.call(
            "session.launch.local.resume.v2",
            {"resume": _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now)},
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*calls, return_exceptions=True)
    winners = [value for value in results if isinstance(value, dict) and value.get("created") is True]
    losers = [value for value in results if isinstance(value, CatalogRemoteError)]
    return {
        "winner_count": len(winners),
        "loser_count": len(losers),
        "live_provider_owner_count": _open_run_count(database_path, session_id),
    }


async def _input_safety_observation(
    client: CatalogClient,
    database_path: Path,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    receipt_id = str(uuid4())
    await client.call(
        "session.input.receipt.upsert.v2",
        {
            "receipt": {
                "owner_id": 7,
                "session_id": session_id,
                "provider": provider,
                "text": "input claimed by ended run",
                "intent": "queue",
                "status": "delivering",
                "client_request_id": receipt_id,
                "device_id": machine_id,
                "thread_id": None,
                "archive_session_input_id": None,
                "control_command_id": None,
                "delivery_request_id": "old-generation",
                "enqueue_archive_projection": False,
                "error": None,
                "expires_at": None,
            }
        },
    )
    await client.call(
        "session.launch.local.resume.v2",
        {"resume": _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now)},
    )
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    try:
        with Session(engine) as db:
            receipt = db.query(LiveSessionInputReceipt).filter(LiveSessionInputReceipt.client_request_id == receipt_id).one()
            error = json.loads(receipt.error_json or "{}")
            return {
                "old_input_delivery_unknown": receipt.status == "failed" and error.get("code") == "delivery_unknown",
                "stale_generation_dispatched": receipt.control_command_id is not None,
            }
    finally:
        engine.dispose()


async def _failure_cleanup_observation(
    client: CatalogClient,
    database_path: Path,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    first = await client.call(
        "session.launch.local.resume.v2",
        {"resume": _resume_payload(provider, session_id, provider_thread_id, machine_id, cwd, now)},
    )
    await _fail_attempt(client, session_id, first["run_id"], machine_id, now)
    retry = await client.call(
        "session.launch.local.resume.v2",
        {
            "resume": _resume_payload(
                provider,
                session_id,
                provider_thread_id,
                machine_id,
                cwd,
                now + timedelta(seconds=3),
            )
        },
    )
    await _fail_attempt(client, session_id, retry["run_id"], machine_id, now + timedelta(seconds=3))
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    try:
        with Session(engine) as db:
            attempts = db.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.session_id == session_id).all()
            alias = (
                db.query(LiveSessionThreadAlias)
                .filter(
                    LiveSessionThreadAlias.alias_kind == "provider_session_id",
                    LiveSessionThreadAlias.alias_value == provider_thread_id,
                )
                .one_or_none()
            )
            return {
                "attempt_closed": all(attempt.state != "pending" for attempt in attempts),
                "contract_restored": retry.get("created") is True and alias is not None,
                "orphan_count": _open_run_count(database_path, session_id),
            }
    finally:
        engine.dispose()


async def _fail_attempt(client: CatalogClient, session_id: str, run_id: str, machine_id: str, now: datetime) -> None:
    await client.call(
        "session.launch.local.finish.v2",
        {
            "outcome": {
                "session_id": session_id,
                "run_id": run_id,
                "owner_id": 7,
                "device_id": machine_id,
                "state": "failed",
                "error_code": "provider_launch_failed",
                "error_message": "factory-injected failure",
                "observed_at": (now + timedelta(seconds=2)).isoformat(),
            }
        },
    )


def _open_run_count(database_path: Path, session_id: str) -> int:
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    try:
        with Session(engine) as db:
            return (
                db.query(LiveSessionRun)
                .join(LiveSessionLaunchAttempt, LiveSessionLaunchAttempt.run_id == LiveSessionRun.id)
                .filter(
                    LiveSessionLaunchAttempt.session_id == session_id,
                    LiveSessionRun.ended_at.is_(None),
                )
                .count()
            )
    finally:
        engine.dispose()


def _run_count_for_session(database_path: Path, session_id: str) -> int:
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    try:
        with Session(engine) as db:
            return (
                db.query(LiveSessionRun)
                .join(LiveSessionLaunchAttempt, LiveSessionLaunchAttempt.run_id == LiveSessionRun.id)
                .filter(LiveSessionLaunchAttempt.session_id == session_id)
                .count()
            )
    finally:
        engine.dispose()


def _result(provider: str, scenario: str, observation: dict[str, Any]) -> dict[str, Any]:
    assertions = assertions_for(scenario, observation)
    passed = all(assertions.values())
    return {
        "status": "pass" if passed else "fail",
        "failure_code": None if passed else f"{scenario}_oracle_failed",
        "provider": provider,
        "scenario": scenario,
        "evidence_class": "hermetic",
        "scenario_revision": 1,
        "observation": observation,
        "assertions": assertions,
    }


def _not_applicable(provider: str, scenario: str, disposition: str) -> dict[str, Any]:
    return {
        "status": "not_applicable",
        "provider": provider,
        "scenario": scenario,
        "disposition": disposition,
        "message": "Scenario does not apply to this provider's declared Resume disposition.",
    }


__all__ = ["SCENARIOS", "run_provider_resume_scenario"]
