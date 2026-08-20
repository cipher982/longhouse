"""An advertised control must be reachable through the served grant.

`test_connection_capability_dispatchability` already forces the schema to agree
with itself: an advertised `can_send_input` must have a contract flag, a
`machine_control_supports` entry, and an engine dispatch path. Antigravity
satisfied every one of those and send still did not work, because nothing
guarded the layer underneath -- whether the runtime path that *writes*
`LiveSessionConnection.can_send_input = 1` can ever actually fire.

It could not. The engine emitted no Antigravity lease, so the server-side
reconciliation that promotes control capability iterated a list the provider
never appeared in, and `live_catalog_launch` re-stamped send as 0 on every
heartbeat. The declaration was consistent at four layers and dead at the fifth.
A human reading the diff caught it; no test did.

So this drives the real ingestion path with the evidence a provider's engine
actually emits and asserts the served grant appears -- the same grant
`catalogd.prepare_control_command` requires before it will accept a queued
input. It is provider-generic on purpose: any provider that advertises a
remote control and cannot reach the grant fails here.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.live_control_catalog import get_live_control_grant
from zerg.services.managed_control_state import upsert_live_control_leases
from zerg.services.managed_local_launcher import managed_provider_requires_readiness_proof
from zerg.services.managed_provider_contracts import all_managed_provider_contracts

DEVICE_ID = "cinder"


@pytest.fixture
def live_engine(tmp_path):
    engine = create_catalog_engine(tmp_path / "live.db")
    initialize_catalog_schema(engine)
    yield engine
    engine.dispose()


def _providers_advertising_send() -> list[str]:
    return sorted(
        contract.provider
        for contract in all_managed_provider_contracts()
        if contract.connection_capabilities.get("can_send_input")
    )


def _seed_live_session(db: Session, provider: str):
    now = datetime.now(UTC).replace(microsecond=0)
    session_id, thread_id, run_id = uuid4(), uuid4(), uuid4()
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
    db.commit()
    return session_id


def _lease(session_id, provider: str):
    """The lease shape the engine publishes in `managed_sessions`."""

    return SimpleNamespace(
        session_id=session_id,
        provider=provider,
        machine_id=DEVICE_ID,
        sequence=1,
        state="attached",
        phase=None,
        tool_name=None,
        bridge_status="ready",
        thread_subscription_status=None,
        observed_at=datetime.now(UTC),
        lease_ttl_ms=900_000,
    )


def _readiness_evidence(session_id, provider: str, *, hook_live: bool):
    """The readiness family the engine ships for hook-delivered providers."""

    if not managed_provider_requires_readiness_proof(provider):
        return None
    return {
        "readiness": [
            {
                "provider": provider,
                "session_id": str(session_id),
                "operation": "send_input",
                "hook_installed": True,
                "recent_hook_observed": hook_live,
            }
        ]
    }


@pytest.mark.parametrize("provider", _providers_advertising_send())
def test_advertised_send_reaches_the_served_grant(provider: str, live_engine) -> None:
    """Feed the provider's own evidence in; the grant must come out."""

    with Session(live_engine) as db:
        session_id = _seed_live_session(db, provider)
        upsert_live_control_leases(
            db,
            [_lease(session_id, provider)],
            device_id=DEVICE_ID,
            received_at=datetime.now(UTC),
            machine_evidence=_readiness_evidence(session_id, provider, hook_live=True),
        )
        db.commit()

        grant = get_live_control_grant(db, session_id=session_id, capability="send")
        assert grant is not None, (
            f"{provider} advertises can_send_input, but feeding the runtime the evidence its "
            f"engine emits produces no served send grant. A client would offer the control and "
            f"catalogd would refuse the input as control_unavailable."
        )


def test_hook_delivered_send_is_withheld_until_the_hook_is_observed(live_engine) -> None:
    """The gate has to bite in the other direction or it is not a gate.

    Antigravity control is hook-delivered and hooks do not fire under every
    credential authority, so a launched, healthy session can be uncontrollable.
    Advertising send there is the failure this whole guard exists to prevent.
    """

    provider = "antigravity"
    assert managed_provider_requires_readiness_proof(provider)
    with Session(live_engine) as db:
        session_id = _seed_live_session(db, provider)
        upsert_live_control_leases(
            db,
            [_lease(session_id, provider)],
            device_id=DEVICE_ID,
            received_at=datetime.now(UTC),
            machine_evidence=_readiness_evidence(session_id, provider, hook_live=False),
        )
        db.commit()
        assert get_live_control_grant(db, session_id=session_id, capability="send") is None


def test_send_is_revoked_when_the_hook_goes_quiet(live_engine) -> None:
    """A capability that latches on is a stale promise, not an observation."""

    provider = "antigravity"
    with Session(live_engine) as db:
        session_id = _seed_live_session(db, provider)
        for hook_live in (True, False):
            upsert_live_control_leases(
                db,
                [_lease(session_id, provider)],
                device_id=DEVICE_ID,
                received_at=datetime.now(UTC),
                machine_evidence=_readiness_evidence(session_id, provider, hook_live=hook_live),
            )
            db.commit()
            grant = get_live_control_grant(db, session_id=session_id, capability="send")
            assert (grant is not None) is hook_live, (
                "send must follow the hook: granted while it is observed, withdrawn when it stops"
            )
        connection = db.query(LiveSessionConnection).one()
        assert connection.can_send_input == 0
