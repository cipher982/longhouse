"""A heartbeat costs what changed, not how many leases the machine holds.

Every heartbeat carries the machine's whole lease snapshot, because absence
detection needs it. Writing and re-attaching every row made one write
proportional to fleet size: on 2026-11-13 the tenant held 1,621 leases for one
builder Mac and 6,862 for the factory machine, and a single
``apply_machine_heartbeat`` held the catalog writer for 142s in
``lease_reconcile``. That stall queued a managed launch's confirmation behind
it, which then read as "Longhouse could not record the launch outcome".

What has to stay true: a lease whose evidence moved still attaches, and an
unchanged lease still refreshes its attachment inside the lease TTL, or an idle
but attached session loses its current claim.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

import zerg.services.live_catalog_launch as live_catalog_launch
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.live_session_state import upsert_live_sessions_from_managed_leases
from zerg.services.managed_control_state import LIVE_ATTACHMENT_REFRESH_INTERVAL
from zerg.services.managed_control_state import live_attachment_health_map
from zerg.services.managed_control_state import upsert_live_control_leases
from zerg.utils.time import normalize_utc


@pytest.fixture
def live_factory(tmp_path: Path):
    engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(engine)
    try:
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()


def _payload(*, bridge_status="ready", thread_subscription_status="active", control_state="online", ttl_ms=60_000):
    return {
        "bridge_status": bridge_status,
        "control_state": control_state,
        "lease_ttl_ms": ttl_ms,
        "observed_at": None,
        "reason": None,
        "thread_subscription_status": thread_subscription_status,
    }


def _seed_attached_sessions(factory, *, provider: str, count: int, attached_at: datetime) -> list[dict]:
    """Sessions whose lease, catalog row, run and attachment already exist."""

    seeded: list[dict] = []
    with factory() as db:
        for index in range(count):
            session_id = uuid4()
            thread_id = uuid4()
            run_id = uuid4()
            db.add(
                LiveSessionCatalog(
                    session_id=str(session_id),
                    provider=provider,
                    environment="development",
                    device_id="cinder",
                    started_at=attached_at - timedelta(minutes=5),
                    primary_thread_id=str(thread_id),
                    created_at=attached_at - timedelta(minutes=5),
                    updated_at=attached_at,
                )
            )
            db.add(
                LiveSessionThread(
                    id=str(thread_id),
                    session_id=str(session_id),
                    provider=provider,
                    branch_kind="root",
                    is_primary=1,
                    created_at=attached_at - timedelta(minutes=5),
                    updated_at=attached_at,
                )
            )
            db.add(
                LiveSessionRun(
                    id=str(run_id),
                    thread_id=str(thread_id),
                    provider=provider,
                    host_id="cinder",
                    launch_origin="longhouse_spawned",
                    started_at=attached_at - timedelta(minutes=5),
                )
            )
            db.add(
                LiveSessionConnection(
                    run_id=str(run_id),
                    control_plane="cursor_helm",
                    acquisition_kind="spawned_control",
                    state="attached",
                    device_id="cinder",
                    acquired_at=attached_at - timedelta(minutes=5),
                    last_health_at=attached_at,
                )
            )
            db.add(
                LiveControlLease(
                    session_id=str(session_id),
                    provider=provider,
                    device_id="cinder",
                    machine_id="cinder",
                    state="attached",
                    sequence=index,
                    heartbeat_at=attached_at,
                    payload_json=json.dumps(_payload(), sort_keys=True),
                    updated_at=attached_at,
                )
            )
            seeded.append(
                {
                    "session_id": str(session_id),
                    "provider": provider,
                    "machine_id": "cinder",
                    "state": "attached",
                    "bridge_status": "ready",
                    "thread_subscription_status": "active",
                    "lease_ttl_ms": 60_000,
                    "sequence": index,
                    "observed_at": attached_at,
                }
            )
        db.commit()
    return seeded


def _lease(row: dict) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _count_attaches(monkeypatch) -> list[str]:
    attached: list[str] = []

    def fake_attach(_db, *, session_id, **_kwargs):
        attached.append(str(session_id))
        return None

    monkeypatch.setattr(live_catalog_launch, "attach_live_catalog_control", fake_attach)
    return attached


def test_unchanged_leases_are_retained_without_reattaching(live_factory, monkeypatch):
    seen_at = datetime.now(timezone.utc)
    seeded = _seed_attached_sessions(live_factory, provider="cursor", count=25, attached_at=seen_at)
    attached = _count_attaches(monkeypatch)

    with live_factory() as db:
        health = live_attachment_health_map(db, [row["session_id"] for row in seeded])
        stats: dict[str, int] = {}
        touched = upsert_live_control_leases(
            db,
            [_lease(row) for row in seeded],
            device_id="cinder",
            received_at=seen_at + timedelta(seconds=5),
            attachment_health=health,
            stats=stats,
        )
        db.commit()
        rows = db.query(LiveControlLease).all()
        heartbeat_ats = {str(row.session_id): row.heartbeat_at for row in rows}

    assert len(touched) == len(seeded)
    assert attached == [], "an unchanged lease must not pay for a full control attach"
    assert stats["leases_present"] == len(seeded)
    assert stats["leases_attached"] == 0
    assert stats["leases_changed"] == 0
    # Retained, not ignored: liveness still advances for every reported lease.
    advanced_at = seen_at + timedelta(seconds=5)
    assert all(normalize_utc(value) >= advanced_at for value in heartbeat_ats.values())
    assert len(heartbeat_ats) == len(seeded)


def test_only_the_lease_whose_evidence_moved_attaches(live_factory, monkeypatch):
    seen_at = datetime.now(timezone.utc)
    seeded = _seed_attached_sessions(live_factory, provider="cursor", count=10, attached_at=seen_at)
    attached = _count_attaches(monkeypatch)
    degraded = dict(seeded[3])
    degraded["state"] = "degraded"
    leases = [_lease(degraded) if index == 3 else _lease(row) for index, row in enumerate(seeded)]

    with live_factory() as db:
        health = live_attachment_health_map(db, [row["session_id"] for row in seeded])
        stats: dict[str, int] = {}
        upsert_live_control_leases(
            db,
            leases,
            device_id="cinder",
            received_at=seen_at + timedelta(seconds=5),
            attachment_health=health,
            stats=stats,
        )
        db.commit()
        row = db.query(LiveControlLease).filter(LiveControlLease.session_id == degraded["session_id"]).one()
        assert row.state == "degraded"

    assert attached == [degraded["session_id"]]
    assert stats["leases_changed"] == 1
    assert stats["leases_attached"] == 1


def test_a_stale_attachment_is_refreshed_even_when_the_lease_is_unchanged(live_factory, monkeypatch):
    seen_at = datetime.now(timezone.utc)
    seeded = _seed_attached_sessions(live_factory, provider="cursor", count=4, attached_at=seen_at)
    attached = _count_attaches(monkeypatch)
    stale = {row["session_id"]: seen_at - LIVE_ATTACHMENT_REFRESH_INTERVAL - timedelta(seconds=1) for row in seeded}

    with live_factory() as db:
        stats: dict[str, int] = {}
        upsert_live_control_leases(
            db,
            [_lease(row) for row in seeded],
            device_id="cinder",
            received_at=seen_at + timedelta(seconds=5),
            attachment_health=stale,
            stats=stats,
        )
        db.commit()

    assert sorted(attached) == sorted(row["session_id"] for row in seeded)
    assert stats["leases_changed"] == 0
    assert stats["leases_refreshed"] == 4
    assert stats["leases_attached"] == 4


def test_an_attachment_one_heartbeat_old_is_not_refreshed(live_factory, monkeypatch):
    """The gate has to clear a whole heartbeat cadence, not match it.

    The first version set the refresh interval to the 60s heartbeat cadence, so
    every attachment was already stale on the next tick and the pass cost
    exactly what it cost before the gate existed.
    """

    seen_at = datetime.now(timezone.utc)
    seeded = _seed_attached_sessions(live_factory, provider="cursor", count=5, attached_at=seen_at - timedelta(seconds=60))
    attached = _count_attaches(monkeypatch)

    with live_factory() as db:
        health = live_attachment_health_map(db, [row["session_id"] for row in seeded])
        stats: dict[str, int] = {}
        upsert_live_control_leases(
            db,
            [_lease(row) for row in seeded],
            device_id="cinder",
            received_at=seen_at,
            attachment_health=health,
            stats=stats,
        )
        db.commit()

    assert attached == []
    assert stats["leases_retained"] == len(seeded)


def test_duplicate_session_in_one_snapshot_is_projected_once(live_factory):
    seen_at = datetime.now(timezone.utc)
    seeded = _seed_attached_sessions(live_factory, provider="cursor", count=1, attached_at=seen_at)
    lease = _lease(seeded[0])

    with live_factory() as db:
        stats: dict[str, int] = {}
        upsert_live_sessions_from_managed_leases(
            db,
            [lease, lease],
            device_id="cinder",
            received_at=seen_at,
            stats=stats,
        )
        db.commit()
        assert stats["sessions_present"] == 2
        assert stats["sessions_changed"] == 1


def test_session_projection_writes_only_what_changed(live_factory):
    seen_at = datetime.now(timezone.utc)
    seeded = _seed_attached_sessions(live_factory, provider="cursor", count=1, attached_at=seen_at)
    leases = [_lease(row) for row in seeded]

    with live_factory() as db:
        # A session the projection has never seen is created and counted.
        created_stats: dict[str, int] = {}
        upsert_live_sessions_from_managed_leases(
            db,
            leases,
            device_id="cinder",
            received_at=seen_at,
            stats=created_stats,
        )
        db.commit()
        assert created_stats["sessions_changed"] == 1

        # The same snapshot again changes nothing, and says so.
        repeated_stats: dict[str, int] = {}
        upsert_live_sessions_from_managed_leases(
            db,
            leases,
            device_id="cinder",
            received_at=seen_at + timedelta(seconds=60),
            stats=repeated_stats,
        )
        db.commit()
        assert repeated_stats["sessions_changed"] == 0
        assert repeated_stats["sessions_present"] == 1
