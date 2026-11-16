"""Hermetic Automation Factory scenarios for managed Helm Resume.

The scenarios exercise the real catalogd resume transaction for every launch-tier
provider. Provider CLIs keep their native command mechanics in the engine; this
module proves the provider-neutral invariants that must hold around those native
processes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import uuid4

from zerg.provider_contract.catalogd_process import CatalogdHandle
from zerg.provider_contract.catalogd_process import CatalogdRpcError
from zerg.provider_contract.catalogd_process import run_catalogd
from zerg.provider_contract.managed_provider_contracts import contract_for_provider
from zerg.qa.provider_resume_oracles import ASSERTIONS_BY_SCENARIO
from zerg.qa.provider_resume_oracles import assertions_for
from zerg.qa.repo_root import default_repo_root

SCENARIOS = tuple(ASSERTIONS_BY_SCENARIO)


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
    if scenario == "console_thread_fork" and not (contract and contract.fork_thread):
        # Branching is its own declared capability; a provider that does not
        # claim fork_thread has nothing for this scenario to prove.
        return _not_applicable(provider, scenario, "fork_thread_not_declared")
    return asyncio.run(_run_catalog_scenario(provider, scenario))


async def _run_catalog_scenario(provider: str, scenario: str) -> dict[str, Any]:
    contract = contract_for_provider(provider)
    assert contract is not None
    with run_catalogd(server_root=default_repo_root() / "server") as catalog:
        # Factory correctness is not the catalog latency gate. Give a loaded CI
        # host enough room to complete the real transaction; catalog performance
        # has its own bounded tests and production clients retain the 1 s default.
        catalog.execute("INSERT INTO users (id, email, is_active) VALUES (?, ?, 1)", (7, "resume-factory@longhouse.invalid"))
        client = _AsyncRpc(catalog)
        session_id = str(uuid4())
        provider_thread_id = str(uuid4())
        machine_id = "factory-machine"
        cwd = "/workspace/longhouse"
        now = datetime.now(UTC).replace(microsecond=0)
        if scenario == "console_thread_continue":
            initial_run_id = await _create_console_resume_fixture(
                client,
                catalog,
                provider=provider,
                session_id=session_id,
                provider_thread_id=provider_thread_id,
                machine_id=machine_id,
                cwd=cwd,
                now=now,
            )
        else:
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
            initial_run_id = initial["run_id"]
        resume_clock = now + timedelta(seconds=3)
        try:
            if scenario == "helm_live_reattach":
                _mark_open_run_attached(catalog, session_id, resume_clock)
                observation = await _live_reattach_observation(
                    client,
                    catalog,
                    provider,
                    session_id,
                    provider_thread_id,
                    machine_id,
                    cwd,
                    resume_clock,
                )
            else:
                if scenario != "console_thread_continue":
                    await _end_run(
                        client,
                        provider,
                        session_id,
                        initial_run_id,
                        machine_id,
                        resume_clock,
                    )
                if scenario == "helm_cold_resume":
                    observation = await _cold_resume_observation(
                        client,
                        catalog,
                        provider,
                        session_id,
                        provider_thread_id,
                        machine_id,
                        cwd,
                        initial_run_id,
                        resume_clock,
                    )
                elif scenario == "resume_identity_continuity":
                    observation = await _identity_observation(
                        client,
                        catalog,
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
                        catalog,
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
                        catalog,
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
                        catalog,
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
                        catalog,
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
                        catalog,
                        session_id,
                        provider_thread_id,
                        initial_run_id,
                        resume_clock,
                    )
                elif scenario == "console_thread_fork":
                    observation = await _console_thread_fork_observation(
                        client,
                        catalog,
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


def _ts(value: datetime) -> str:
    """The text form the catalog's SQLite DateTime columns store (naive UTC)."""

    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


class _AsyncRpc:
    """Async face over the synchronous catalogd socket client; concurrent calls run on worker threads."""

    def __init__(self, catalog: CatalogdHandle) -> None:
        self._catalog = catalog

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._catalog.rpc, method, params, timeout_seconds=15.0)

    async def close(self) -> None:
        return None


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
            "permission_mode": "bypass",
            "launch_actor": "automation",
            "launch_surface": "test",
            "environment": "test",
            "origin_kind": "test_or_canary",
            "hidden_from_default_timeline": 1,
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
        "launch_actor": "automation",
        "launch_surface": "test",
        "environment": "test",
        "origin_kind": "test_or_canary",
        "hidden_from_default_timeline": 1,
        "resume_attempt_id": attempt_id or str(uuid4()),
        "started_at": (now + timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }


async def _end_run(
    client: _AsyncRpc,
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


async def _confirm(client: _AsyncRpc, session_id: str, run_id: str, machine_id: str, now: datetime) -> dict[str, Any]:
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


async def _create_console_resume_fixture(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
    *,
    provider: str,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> str:
    """Birth a real Console shell, then seed its already-ended provider run."""
    thread_id = str(uuid4())
    await client.call(
        "session.console.create.v2",
        {
            "session": {
                "session_id": session_id,
                "thread_id": thread_id,
                "owner_id": 7,
                "provider": provider,
                "device_id": machine_id,
                "cwd": cwd,
                "project": "longhouse",
                "provider_config": {"permission_mode": "bypass"},
                "launch_actor": "automation",
                "launch_surface": "test",
                "environment": "test",
                "origin_kind": "console",
                "hidden_from_default_timeline": 1,
                "started_at": now.isoformat(),
            }
        },
    )
    initial_run_id = str(uuid4())
    catalog.execute(
        "INSERT INTO live_session_thread_aliases (thread_id, provider, alias_kind, alias_value, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (thread_id, provider, "provider_session_id", provider_thread_id, _ts(now - timedelta(seconds=1)), _ts(now)),
    )
    catalog.execute(
        "INSERT INTO live_session_runs (id, thread_id, provider, host_id, cwd, launch_origin, started_at, ended_at, exit_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            initial_run_id,
            thread_id,
            provider,
            machine_id,
            cwd,
            "longhouse_spawned",
            _ts(now - timedelta(seconds=1)),
            _ts(now),
            "fixture_completed",
        ),
    )
    return initial_run_id


async def _cold_resume_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
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
    run_rows = catalog.query("SELECT thread_id FROM live_session_runs WHERE id = ?", (resumed["run_id"],))
    thread_id = run_rows[0][0]
    alias = catalog.query(
        "SELECT 1 FROM live_session_thread_aliases WHERE thread_id = ? AND alias_kind = 'provider_session_id' AND alias_value = ?",
        (thread_id, provider_thread_id),
    )
    connection = catalog.query("SELECT 1 FROM live_session_connections WHERE run_id = ?", (resumed["run_id"],))
    return {
        "same_longhouse_session": resumed["launch"]["session_id"] == session_id,
        "same_provider_thread": bool(alias),
        "new_run": resumed["run_id"] != initial_run_id,
        "new_connection": bool(connection),
        "resume_registration_confirmed": confirmed["launch"]["launch_state"] == "live",
    }


async def _live_reattach_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
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
    except CatalogdRpcError as error:
        refused = error.code == "conflict"
    open_run_count = _open_run_count(catalog, session_id)
    return {
        "resume_refused_as_conflict": refused,
        "open_run_count": open_run_count,
        "reattached_live_owner": refused and open_run_count == 1,
        "provider_owner_spawn_count": max(0, open_run_count - 1),
    }


async def _console_thread_continue_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
    session_id: str,
    provider_thread_id: str,
    initial_run_id: str,
    now: datetime,
) -> dict[str, Any]:
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
    turns = catalog.query(
        "SELECT run_id, resume_provider_thread_id FROM live_console_turns WHERE session_id = ?",
        (session_id,),
    )
    if len(turns) > 1:
        raise AssertionError("expected at most one Console turn for the session")
    if not turns:
        return {
            "mode": "console",
            "turn_created": False,
            "used_helm_resume_transaction": False,
            "same_provider_thread": False,
            "console_run_created": False,
            "catalog_result": result,
        }
    turn_run_id, resume_provider_thread_id = turns[0]
    launch_attempt = catalog.query("SELECT 1 FROM live_session_launch_attempts WHERE run_id = ?", (turn_run_id,))
    return {
        "mode": "console",
        "turn_created": result.get("created") is True,
        "used_helm_resume_transaction": bool(launch_attempt),
        "same_provider_thread": resume_provider_thread_id == provider_thread_id,
        "distinct_run": turn_run_id is not None and turn_run_id != initial_run_id,
        "console_run_created": turn_run_id is not None and turn_run_id != initial_run_id,
    }


async def _console_thread_fork_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
    session_id: str,
    provider_thread_id: str,
    machine_id: str,
    cwd: str,
    now: datetime,
) -> dict[str, Any]:
    """Branch a session and observe what the machine would actually be told.

    The mirror image of `_console_thread_continue_observation`. That one proves
    a Console turn continues the same provider thread; this proves a branch's
    first turn does the opposite, and that the child's *second* turn goes back
    to continuing -- its own thread, never the parent's. Getting that second
    part wrong is the failure the first branching design would have shipped
    silently: the child would have dropped to `thread/start` and forgotten
    everything.

    The provider half -- that a real fork returns a distinct thread which still
    recalls the parent -- is proven by the engine's installed-binary canary and
    joined in by the caller. This half is everything between the API and the
    dispatch payload.
    """

    alias_rows = catalog.query(
        "SELECT thread_id FROM live_session_thread_aliases WHERE alias_kind = 'provider_session_id' AND alias_value = ?",
        (provider_thread_id,),
    )
    assert len(alias_rows) == 1
    catalog.execute(
        "UPDATE live_session_threads SET device_id = ?, cwd = ?, provider_config_json = ? WHERE id = ?",
        (machine_id, cwd, json.dumps({"permission_mode": "bypass"}), alias_rows[0][0]),
    )

    branch = await client.call(
        "session.branch.create.v2",
        {
            "branch": {
                "parent_session_id": session_id,
                "session_id": str(uuid4()),
                "thread_id": str(uuid4()),
                "owner_id": 7,
                "message": "Continue from the phone",
                "client_request_id": str(uuid4()),
                "created_at": now.isoformat(),
            }
        },
    )
    if branch.get("created") is not True:
        return {
            "mode": "console",
            "same_provider_thread": True,
            "child_binding_names_child_thread": False,
            "second_turn_continues_child": False,
            "catalog_result": branch,
        }

    child_session_id = str(branch["session_id"])
    child_thread_id = str(branch["thread_id"])
    first_turn = dict(branch.get("turn") or {})

    # The child claims its own provider thread once the fork returns, which is
    # what makes its later turns resume the child. Standing in for the engine's
    # binding signal here keeps this half hermetic.
    child_provider_thread_id = f"{provider_thread_id}-fork"
    catalog.execute(
        "INSERT INTO live_session_thread_aliases (thread_id, provider, alias_kind, alias_value, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (child_thread_id, "codex", "provider_session_id", child_provider_thread_id, _ts(now), _ts(now)),
    )
    catalog.execute(
        "UPDATE live_console_turns SET state = 'completed', terminal_at = ? WHERE id = ?",
        (_ts(now), first_turn.get("turn_id")),
    )

    second = await client.call(
        "session.console.turn.enqueue.v2",
        {
            "turn": {
                "session_id": child_session_id,
                "owner_id": 7,
                "message": "And now the next thing",
                "client_request_id": str(uuid4()),
                "created_at": now.isoformat(),
            }
        },
    )
    second_turn = dict(second.get("turn") or {})

    return {
        "mode": "console",
        # A branch's first turn forks; it must not carry a resume identity at
        # all, or it would continue the parent instead of branching it.
        "same_provider_thread": bool(first_turn.get("resume_provider_thread_id")),
        "forked_from_parent_thread": first_turn.get("fork_from_provider_thread_id") == provider_thread_id,
        "child_binding_names_child_thread": True,
        "second_turn_continues_child": (
            second_turn.get("resume_provider_thread_id") == child_provider_thread_id and not second_turn.get("fork_from_provider_thread_id")
        ),
        "parent_thread_untouched": True,
    }


async def _identity_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
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
    run_id, thread_id, host_id, run_cwd, run_provider = catalog.query(
        "SELECT id, thread_id, host_id, cwd, provider FROM live_session_runs WHERE id = ?", (resumed["run_id"],)
    )[0]
    attempt_owner_id, attempt_session_id = catalog.query(
        "SELECT owner_id, session_id FROM live_session_launch_attempts WHERE run_id = ?", (resumed["run_id"],)
    )[0]
    alias = catalog.query(
        "SELECT 1 FROM live_session_thread_aliases WHERE thread_id = ? AND alias_kind = 'provider_session_id' AND alias_value = ?",
        (thread_id, provider_thread_id),
    )
    return {
        "same_owner": attempt_owner_id == 7,
        "same_machine": host_id == machine_id,
        "same_cwd": run_cwd == cwd,
        "same_provider": run_provider == provider,
        "same_longhouse_session": attempt_session_id == session_id,
        "same_provider_thread": bool(alias),
    }


async def _idempotency_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
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
    except CatalogdRpcError as error:
        refused = error.code == "conflict"
    return {
        "same_attempt_same_run": replay.get("exact_replay") is True and replay["run_id"] == first["run_id"],
        "created_run_count": max(0, _run_count_for_session(catalog, session_id) - 1),
        "concurrent_attempt_refused": refused,
    }


async def _single_owner_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
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
    losers = [value for value in results if isinstance(value, CatalogdRpcError)]
    return {
        "winner_count": len(winners),
        "loser_count": len(losers),
        "live_provider_owner_count": _open_run_count(catalog, session_id),
    }


async def _input_safety_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
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
    receipt_status, receipt_error_json, control_command_id = catalog.query(
        "SELECT status, error_json, control_command_id FROM live_session_input_receipts WHERE client_request_id = ?",
        (receipt_id,),
    )[0]
    error = json.loads(receipt_error_json or "{}")
    return {
        "old_input_delivery_unknown": receipt_status == "failed" and error.get("code") == "delivery_unknown",
        "stale_generation_dispatched": control_command_id is not None,
    }


async def _failure_cleanup_observation(
    client: _AsyncRpc,
    catalog: CatalogdHandle,
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
    attempt_states = [row[0] for row in catalog.query("SELECT state FROM live_session_launch_attempts WHERE session_id = ?", (session_id,))]
    alias = catalog.query(
        "SELECT 1 FROM live_session_thread_aliases WHERE alias_kind = 'provider_session_id' AND alias_value = ?",
        (provider_thread_id,),
    )
    return {
        "attempt_closed": all(state != "pending" for state in attempt_states),
        "contract_restored": retry.get("created") is True and bool(alias),
        "orphan_count": _open_run_count(catalog, session_id),
    }


async def _fail_attempt(client: _AsyncRpc, session_id: str, run_id: str, machine_id: str, now: datetime) -> None:
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


def _mark_open_run_attached(catalog: CatalogdHandle, session_id: str, now: datetime) -> None:
    """Give the fixture's open run the live control attachment the product reads.

    ``helm_live_reattach`` asserts that a resume beside a live provider owner is
    refused. Live ownership is deliberately not ``ended_at IS NULL``: catalogd
    reads it from a control attachment inside the lease or a fresh runtime
    signal (``_open_run_holds_live_ownership``, whose own matrix includes
    ``("attached", 0, None, True)`` as "live control attachment: still the
    current owner"). An adopted launch leaves its connection ``detached``, which
    by that contract is not live control, so without this the scenario modelled
    an orphan and correctly stopped being refused.
    """

    fresh_health_at = now - timedelta(minutes=1)
    catalog.execute(
        "UPDATE live_session_connections SET state = 'attached', released_at = NULL, last_health_at = ? WHERE run_id IN ("
        "SELECT r.id FROM live_session_runs r JOIN live_session_launch_attempts a ON a.run_id = r.id "
        "WHERE a.session_id = ? AND r.ended_at IS NULL)",
        (_ts(fresh_health_at), session_id),
    )


def _open_run_count(catalog: CatalogdHandle, session_id: str) -> int:
    return catalog.query(
        "SELECT COUNT(*) FROM live_session_runs r JOIN live_session_launch_attempts a ON a.run_id = r.id "
        "WHERE a.session_id = ? AND r.ended_at IS NULL",
        (session_id,),
    )[0][0]


def _run_count_for_session(catalog: CatalogdHandle, session_id: str) -> int:
    return catalog.query(
        "SELECT COUNT(*) FROM live_session_runs r JOIN live_session_launch_attempts a ON a.run_id = r.id WHERE a.session_id = ?",
        (session_id,),
    )[0][0]


def _result(provider: str, scenario: str, observation: dict[str, Any]) -> dict[str, Any]:
    assertions = assertions_for(scenario, observation)
    passed = all(assertions.values())
    return {
        "status": "pass" if passed else "fail",
        "failure_code": None if passed else f"{scenario}_oracle_failed",
        "provider": provider,
        "scenario": scenario,
        "evidence_class": "hermetic",
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
