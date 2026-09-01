"""An advertised control must be reachable through the served grant.

`test_connection_capability_dispatchability` forces the schema to agree with
itself: an advertised `can_send_input` must have a contract flag, a
`machine_control_supports` entry, and an engine dispatch path. Antigravity
satisfied every one of those and send still did not work, because nothing
guarded the layer underneath -- whether a provider can actually produce the
bound control identity and control fact that `get_live_control_grant` requires.

It could not: the engine emitted no Antigravity control fact at all, so the
declaration was consistent at four layers and dead at the fifth. A human
reading the diff caught it; no test did.

So this drives the authorization path itself. For every provider that
advertises a remote control, it binds the identity that provider's engine
supplies and reduces the control fact that engine emits, then asserts the grant
appears -- the same grant `catalogd.prepare_control_command` demands before it
will accept a queued input.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.live_control_catalog import canonical_command_authorization_providers
from zerg.services.live_control_catalog import canonical_live_control_capabilities
from zerg.services.live_control_catalog import get_canonical_live_control_grant
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.managed_provider_contracts import contract_for_provider

DEVICE_ID = "cinder"

# Contract capability column -> (engine-granted operation, grant capability name)
_REMOTE_CONTROLS = {
    "can_send_input": ("send_input", "send"),
    "can_interrupt": ("interrupt", "interrupt"),
    "can_terminate": ("terminate", "terminate"),
}


@pytest.fixture
def live_engine(tmp_path):
    engine = create_catalog_engine(tmp_path / "live.db")
    initialize_catalog_schema(engine)
    yield engine
    engine.dispose()


def _advertised_controls() -> list[tuple[str, str, str]]:
    """Every (provider, operation, capability) a client would be offered.

    Deliberately unfiltered by canonical authorization: a provider that drifts
    outside it must fail the driving test below with a concrete grant reason,
    not quietly drop out of the parametrization.
    """

    return sorted(
        (contract.provider, operation, capability)
        for contract in all_managed_provider_contracts()
        for column, (operation, capability) in _REMOTE_CONTROLS.items()
        if contract.connection_capabilities.get(column)
    )


def test_every_advertised_machine_control_is_canonically_servable() -> None:
    """The general invariant: nothing in `machine_control_supports` may be unservable.

    `get_canonical_live_control_grant` refuses any provider outside
    `_CANONICAL_AUTH_PROVIDERS` with `unsupported`, before it looks at a single
    fact. So a live-control entry in `machine_control_supports` from outside
    that set is a dead declaration: the contract advertises it, the launcher
    writes `can_send_input`/`can_interrupt`/`can_terminate` onto the born
    connection, the client renders a composer with Interrupt and Terminate, and
    every press fails at authorization.

    Pi shipped exactly that. `longhouse pi` registers a Console one-shot and
    enqueues `session.turn.start`; there is no long-running pi session, and
    `control_channel.rs` has no pi branch for `session.send_text` or
    `session.terminate`. It advertised `pi.send`, `pi.interrupt` and
    `pi.terminate` anyway.

    Both sides of this are derived, not curated: the capability set comes from
    `canonical_live_control_capabilities()` and the provider set from
    `canonical_command_authorization_providers()`, so a new capability or a new
    provider is covered the day it lands.
    """

    canonical_providers = set(canonical_command_authorization_providers())
    live_control_capabilities = set(canonical_live_control_capabilities())
    unservable = sorted(
        support
        for contract in all_managed_provider_contracts()
        for support in contract.machine_control_supports
        if support.partition(".")[2] in live_control_capabilities and contract.provider not in canonical_providers
    )
    assert unservable == [], (
        f"{unservable} advertise live control that canonical authorization refuses outright. "
        "Either the provider belongs in _CANONICAL_AUTH_PROVIDERS with an adapter that emits "
        "control facts, or the contract must stop claiming the operation."
    )


def test_turn_scoped_supports_are_not_subject_to_canonical_authorization() -> None:
    """The invariant above must not over-reach, or it deletes working Console controls.

    Console turns (`session.turn.start` / `session.turn.interrupt`) never reach
    `get_canonical_live_control_grant` -- `managed_control_dispatcher` prepares a
    catalog operation only for send/steer/answer_pause/interrupt/terminate. Pi is
    the live case: it keeps `pi.turn_start` and `pi.turn_interrupt` from outside
    canonical authorization, and those really are served.
    """

    live_control_capabilities = set(canonical_live_control_capabilities())
    assert "turn_start" not in live_control_capabilities
    assert "turn_interrupt" not in live_control_capabilities

    pi = contract_for_provider("pi")
    assert pi is not None
    assert pi.provider not in set(canonical_command_authorization_providers())
    assert set(pi.machine_control_supports) == {"pi.turn_start", "pi.turn_interrupt"}
    assert pi.connection_capabilities == {
        "can_send_input": 0,
        "can_interrupt": 0,
        "can_terminate": 0,
        "can_tail_output": 1,
        "can_resume": 0,
    }


def _seed_bound_session(engine, provider: str):
    """A live session carrying the control identity its engine would supply."""

    now = datetime.now(UTC).replace(microsecond=0)
    session_id, thread_id, run_id = uuid4(), uuid4(), uuid4()
    connection_id, lease_generation = str(uuid4()), str(uuid4())
    caps = contract_for_provider(provider).connection_capabilities
    control_plane = contract_for_provider(provider).control_plane
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider=provider,
                environment="production",
                device_id=DEVICE_ID,
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
                host_id=DEVICE_ID,
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
        db.add(
            LiveSessionConnection(
                run_id=str(run_id),
                adapter_connection_id=connection_id,
                lease_generation=lease_generation,
                control_plane=control_plane,
                acquisition_kind="spawned_control",
                state="attached",
                device_id=DEVICE_ID,
                can_send_input=caps["can_send_input"],
                can_interrupt=caps["can_interrupt"],
                can_terminate=caps["can_terminate"],
                can_tail_output=caps["can_tail_output"],
                can_resume=caps["can_resume"],
                acquired_at=now,
                last_health_at=now,
            )
        )
        db.commit()
    return session_id, run_id, connection_id, lease_generation


def _reduce_control_fact(engine, *, provider, session_id, run_id, connection_id, lease_generation, grants):
    observed_at = datetime.now(UTC).replace(microsecond=0)
    value = {
        "authority_class": "provider_control",
        "provider": provider,
        "session_id": str(session_id),
        "run_id": str(run_id),
        "connection_id": connection_id,
        "lease_generation": lease_generation,
        "granted_operations": list(grants),
        "state": "attached" if grants else "detached",
        "lease_ttl_ms": 900_000,
        "source": f"{provider}_control_scan",
        "observed_at": observed_at.isoformat(),
    }
    fact = ReducerFact(
        family="control",
        subject_key=f"connection:{connection_id}:{lease_generation}",
        source=f"{provider}_control_scan",
        source_epoch=lease_generation,
        source_seq=None,
        dedupe_key=canonical_evidence_hash({**value, "dedupe": observed_at.isoformat()}),
        evidence_hash=canonical_evidence_hash(value),
        value=value,
        observed_at=observed_at,
        session_id=str(session_id),
    )
    with engine.begin() as connection:
        reduce_fact_batch(connection, [fact], received_at=observed_at)


@pytest.mark.parametrize(("provider", "operation", "capability"), _advertised_controls())
def test_advertised_control_reaches_the_served_grant(
    provider: str, operation: str, capability: str, live_engine
) -> None:
    """Bind the identity, reduce the fact, and the grant must come out."""

    session_id, run_id, connection_id, lease_generation = _seed_bound_session(live_engine, provider)
    _reduce_control_fact(
        live_engine,
        provider=provider,
        session_id=session_id,
        run_id=run_id,
        connection_id=connection_id,
        lease_generation=lease_generation,
        grants=[operation],
    )
    with Session(live_engine) as db:
        grant, reason = get_canonical_live_control_grant(
            db,
            session_id=session_id,
            provider=provider,
            device_id=DEVICE_ID,
            capability=capability,
            now=datetime.now(UTC),
        )
    assert grant is not None, (
        f"{provider} advertises {operation}, but binding the identity its engine supplies and "
        f"reducing the control fact its engine emits produces no served grant ({reason}). A "
        f"client would offer this control and catalogd would refuse the command."
    )


def test_a_control_the_engine_does_not_grant_is_not_served(live_engine) -> None:
    """The gate has to bite in the other direction or it is not a gate.

    Antigravity is the case this matters for: control is hook-delivered, and
    hooks do not fire under every credential authority, so a launched, healthy
    session can be uncontrollable. Its engine reports that by granting nothing,
    and the served surface has to follow rather than trust the contract.
    """

    session_id, run_id, connection_id, lease_generation = _seed_bound_session(live_engine, "antigravity")
    _reduce_control_fact(
        live_engine,
        provider="antigravity",
        session_id=session_id,
        run_id=run_id,
        connection_id=connection_id,
        lease_generation=lease_generation,
        grants=[],
    )
    with Session(live_engine) as db:
        grant, reason = get_canonical_live_control_grant(
            db,
            session_id=session_id,
            provider="antigravity",
            device_id=DEVICE_ID,
            capability="send",
            now=datetime.now(UTC),
        )
    assert grant is None
    assert reason == "not_granted"


def test_an_unbound_session_is_never_granted(live_engine) -> None:
    """No identity, no authorization.

    A Shadow session has no launcher to seed control identity, so it can never
    reach a grant however healthy it looks -- even with the capability columns
    set. That is the whole reason the identity requirement exists, so it earns
    a test of its own rather than riding on the cases above.
    """

    now = datetime.now(UTC).replace(microsecond=0)
    session_id, thread_id, run_id = uuid4(), uuid4(), uuid4()
    with Session(live_engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="antigravity",
                environment="production",
                device_id=DEVICE_ID,
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
                provider="antigravity",
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
                provider="antigravity",
                host_id=DEVICE_ID,
                launch_origin="user_spawned",
                started_at=now,
            )
        )
        db.add(
            LiveSessionConnection(
                run_id=str(run_id),
                adapter_connection_id=None,
                lease_generation=None,
                control_plane="antigravity_hook_inbox",
                acquisition_kind="observed",
                state="attached",
                device_id=DEVICE_ID,
                can_send_input=1,
                can_interrupt=0,
                can_terminate=0,
                can_tail_output=1,
                can_resume=0,
                acquired_at=now,
                last_health_at=now,
            )
        )
        db.commit()
        grant, reason = get_canonical_live_control_grant(
            db,
            session_id=session_id,
            provider="antigravity",
            device_id=DEVICE_ID,
            capability="send",
            now=datetime.now(UTC),
        )
    assert grant is None
    assert reason == "identity_unbound"
