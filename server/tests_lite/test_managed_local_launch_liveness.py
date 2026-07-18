"""Liveness honesty for the interactive managed-local launch path.

These tests pin the fix that ``live_control_available`` must mean "an
observer measured a ready control channel recently", not "the launcher
asserted it at row birth". See the launcher (``managed_local_launcher``),
the read-time freshness clamp (``kernel_capabilities``), and the heartbeat
reconciler (``managed_control_state``).

Per tests_lite convention: per-test SQLite, no shared conftest.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.managed_control_state import DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS
from zerg.services.managed_control_state import upsert_managed_control_leases
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import launch_managed_local_session_sync


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_managed_local_launch_liveness.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_user_and_runner(db, *, device_name: str = "cinder"):
    user = User(email="managed-local-liveness@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    runner = Runner(
        owner_id=user.id,
        name=device_name,
        availability_policy="always_on",
        capabilities=["exec.full"],
        status="online",
        auth_secret_hash="secret-hash",
        runner_metadata={"install_mode": "desktop"},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return user, runner


def _launch(db, *, owner_id: int, device_name: str, provider: str = "claude"):
    params = ManagedLocalLaunchParams(
        owner_id=owner_id,
        runner_target=device_name,
        cwd="/Users/example/git/zerg",
        provider=provider,
        machine_name=device_name,
        native_claude_channels_available=True,
    )
    return launch_managed_local_session_sync(db, params)


def test_i1_launch_is_not_live_until_observer_promotes_same_row(tmp_path):
    """I1: launch is born reattach-available (not live); a ready lease flips it
    to live ON THE SAME connection_id AND run_id."""

    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db, device_name="cinder")

        result = _launch(db, owner_id=user.id, device_name=runner.name, provider="claude")
        session = result.session

        # Born honest: control path exists but no observer confirmed it ready.
        caps = project_session_capabilities(db, session_id=session.id)
        assert caps.live_control_available is False
        assert caps.host_reattach_available is True
        assert caps.control_label == "reattach"

        # Capture the launcher's own connection + run identity from the
        # projection (the freshness clamp does not hide a detached row's ids).
        born_connection_id = caps.connection_id
        born_run_id = caps.run_id
        assert born_connection_id is not None
        assert born_run_id is not None

        # The engine observes the bridge ready ~1s later: a synthetic attached
        # lease through the heartbeat reconciler must promote the SAME row.
        upsert_managed_control_leases(
            db,
            [
                SimpleNamespace(
                    session_id=session.id,
                    provider="claude",
                    state="attached",
                    bridge_status="ready",
                    thread_subscription_status="subscribed",
                    machine_id=runner.name,
                )
            ],
            device_id=runner.name,
            received_at=datetime.now(timezone.utc),
        )
        db.commit()

        promoted = project_session_capabilities(db, session_id=session.id)
        assert promoted.live_control_available is True
        assert promoted.control_label == "live"
        # Promotion landed on the launcher's row — no duplicate/orphan.
        assert promoted.connection_id == born_connection_id
        assert promoted.run_id == born_run_id
        assert db.query(SessionConnection).filter(SessionConnection.run_id == born_run_id).count() == 1


def test_antigravity_launch_does_not_grant_send_before_hook_proof(tmp_path):
    """Dispatching the Antigravity binary is not hook-inbox readiness proof."""

    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db, device_name="cinder")
        result = _launch(db, owner_id=user.id, device_name=runner.name, provider="antigravity")
        session = result.session

        conn = db.query(SessionConnection).one()
        assert conn.state == "detached"
        assert conn.can_send_input == 0
        assert conn.last_health_at is None

        caps = project_session_capabilities(db, session_id=session.id)
        assert caps.live_control_available is False
        assert caps.can_send_input is False
        assert caps.control_label == "reattach"

        # A legacy attached lease is process/control evidence, not hook-inbox
        # readiness. It may make the connection live, but cannot grant send.
        upsert_managed_control_leases(
            db,
            [
                SimpleNamespace(
                    session_id=session.id,
                    provider="antigravity",
                    state="attached",
                    bridge_status="ready",
                    thread_subscription_status=None,
                    machine_id=runner.name,
                )
            ],
            device_id=runner.name,
            received_at=datetime.now(timezone.utc),
        )
        db.commit()
        legacy_lease_caps = project_session_capabilities(db, session_id=session.id)
        assert legacy_lease_caps.live_control_available is True
        assert legacy_lease_caps.can_send_input is False


def test_opencode_is_born_detached_pending_lease_observation(tmp_path):
    """OpenCode now ships heartbeat leases, so it is born ``detached`` and
    promoted to ``attached`` only once the engine observes a live server. This
    prevents a birth-time ``attached`` from claiming live control during the
    window where the server bridge could still fail to start."""

    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db, device_name="cinder")
        result = _launch(db, owner_id=user.id, device_name=runner.name, provider="opencode")
        session = result.session

        conn = db.query(SessionConnection).one()
        assert conn.state == "detached"

        caps = project_session_capabilities(db, session_id=session.id)
        assert caps.live_control_available is False


def test_i2_attached_connection_past_ttl_is_not_live(tmp_path):
    """I2: an attached connection whose last_health_at predates the lease TTL
    projects live_control_available=False (reattach), enforced at read time."""

    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db, device_name="cinder")
        result = _launch(db, owner_id=user.id, device_name=runner.name, provider="claude")
        session = result.session

        # Promote to live with a ready lease.
        upsert_managed_control_leases(
            db,
            [
                SimpleNamespace(
                    session_id=session.id,
                    provider="claude",
                    state="attached",
                    bridge_status="ready",
                    thread_subscription_status="subscribed",
                    machine_id=runner.name,
                )
            ],
            device_id=runner.name,
            received_at=datetime.now(timezone.utc),
        )
        db.commit()
        assert project_session_capabilities(db, session_id=session.id).live_control_available is True

        # Backdate the health stamp past the TTL (slept laptop / dead engine).
        conn = db.query(SessionConnection).one()
        ttl = timedelta(milliseconds=DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS)
        conn.last_health_at = datetime.now(timezone.utc) - ttl - timedelta(seconds=60)
        db.commit()

        caps = project_session_capabilities(db, session_id=session.id)
        assert caps.live_control_available is False
        assert caps.host_reattach_available is True
        assert caps.control_label == "reattach"


def test_i2_null_health_attached_is_not_live(tmp_path):
    """I2 (NULL variant): a stuck-attached row with NULL last_health_at — the
    legacy birth-time optimistic shape — never projects live."""

    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user, runner = _seed_user_and_runner(db, device_name="cinder")
        result = _launch(db, owner_id=user.id, device_name=runner.name, provider="claude")
        session = result.session

        # Simulate the legacy bug shape directly: attached + NULL health.
        conn = db.query(SessionConnection).one()
        conn.state = "attached"
        conn.last_health_at = None
        db.commit()

        caps = project_session_capabilities(db, session_id=session.id)
        assert caps.live_control_available is False
        assert caps.host_reattach_available is True


def test_i4_observe_only_with_send_bit_is_not_live(tmp_path):
    """I4: a log_tail observe_only connection with can_send_input=1 (and fresh
    health) still projects live=False — the acquisition_kind gate holds."""

    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user, _runner = _seed_user_and_runner(db, device_name="cinder")
        session = AgentSession(
            id=uuid4(),
            provider="claude",
            environment="development",
            project="zerg",
            device_id="cinder",
            cwd="/Users/example/git/zerg",
            started_at=datetime.now(timezone.utc),
                                                user_messages=0,
            assistant_messages=0,
            tool_calls=0,
        )
        db.add(session)
        db.commit()

        from zerg.services.agents.kernel_writes import ensure_open_run_for_session
        from zerg.services.agents.kernel_writes import record_connection

        run = ensure_open_run_for_session(db, session)
        record_connection(
            db,
            run=run,
            control_plane="log_tail",
            acquisition_kind="observe_only",
            state="attached",
            can_send_input=1,
            can_tail_output=1,
        )
        # Fresh health so the freshness clamp does NOT explain the non-live result.
        conn = db.query(SessionConnection).one()
        conn.last_health_at = datetime.now(timezone.utc)
        db.commit()

        caps = project_session_capabilities(db, session_id=session.id)
        assert caps.live_control_available is False
        assert caps.observe_only is True
        assert caps.search_only is False
