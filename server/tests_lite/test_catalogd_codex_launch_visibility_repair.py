from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import FactHead
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.machine_evidence import canonical_evidence_hash
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-codex-visibility-repair-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_eligible_session(
    database_path: Path,
    *,
    user_hidden: bool = False,
    thread_hidden: bool = True,
) -> str:
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    thread_id = str(uuid4())
    run_id = str(uuid4())
    connection_id = str(uuid4())
    lease_generation = str(uuid4())
    control_value = {
        "authority_class": "provider_control",
        "provider": "codex",
        "session_id": session_id,
        "run_id": run_id,
        "connection_id": connection_id,
        "lease_generation": lease_generation,
        "granted_operations": ["interrupt", "send_input", "terminate"],
        "state": "attached",
        "terminal_attached": True,
        "lease_ttl_ms": 120_000,
        "source": "provider_control",
        "observed_at": now.isoformat(),
    }
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=session_id,
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                primary_thread_id=thread_id,
                origin_kind=None,
                hidden_from_default_timeline=1,
                user_hidden_from_timeline=int(user_hidden),
                launch_actor=None,
                launch_surface=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveTimelineCard(
                session_id=session_id,
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                parser_revision="test",
                origin_kind=None,
                hidden_from_default_timeline=1,
                user_hidden_from_timeline=int(user_hidden),
                launch_actor=None,
                launch_surface=None,
                updated_at=now,
            )
        )
        db.add(
            LiveSessionThread(
                id=thread_id,
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                cwd="/workspace/longhouse",
                branch_kind="root",
                origin_kind=None,
                hidden_from_default_timeline=int(thread_hidden),
                is_primary=1,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveSessionRun(
                id=run_id,
                thread_id=thread_id,
                provider="codex",
                host_id="cinder",
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
        db.add(
            LiveSessionConnection(
                run_id=run_id,
                adapter_connection_id=connection_id,
                lease_generation=lease_generation,
                control_plane="codex_app_server",
                acquisition_kind="spawned_control",
                state="attached",
                device_id="cinder",
                can_send_input=1,
                can_interrupt=1,
                can_terminate=1,
                can_tail_output=1,
                can_resume=1,
                acquired_at=now,
                last_health_at=now,
            )
        )
        db.add(
            FactHead(
                family="control",
                subject_key=f"connection:{connection_id}:{lease_generation}",
                source="provider_control",
                source_epoch=lease_generation,
                session_id=session_id,
                ordering_mode="observed_at",
                evidence_hash=canonical_evidence_hash(control_value),
                observed_at=now,
                valid_until=now + timedelta(minutes=2),
                value_json=json.dumps(control_value),
                updated_commit_seq=1,
                received_at=now,
            )
        )
        db.commit()
    engine.dispose()
    return session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_hidden", [True, False])
async def test_codex_visibility_repair_is_dry_run_first_cas_and_idempotent(
    daemon_paths,
    thread_hidden: bool,
):
    database_path, socket_path = daemon_paths
    session_id = _seed_eligible_session(database_path, thread_hidden=thread_hidden)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        dry_run = await client.call(
            "session.repair.codex_launch_visibility.v2",
            {"session_id": session_id, "dry_run": True, "expected_fingerprint": None},
        )
        assert dry_run["eligible"] is True
        assert dry_run["applied"] is False
        assert dry_run["updates"] == {
            "launch_actor": "human_shell",
            "launch_surface": "terminal",
            "hidden_from_default_timeline": False,
        }

        conflict = await client.call(
            "session.repair.codex_launch_visibility.v2",
            {"session_id": session_id, "dry_run": False, "expected_fingerprint": "0" * 64},
        )
        assert conflict["applied"] is False
        assert conflict["refusals"] == ["compare_and_set_failed"]
        assert conflict["commit_seq"] == dry_run["commit_seq"]

        applied = await client.call(
            "session.repair.codex_launch_visibility.v2",
            {
                "session_id": session_id,
                "dry_run": False,
                "expected_fingerprint": dry_run["expected_fingerprint"],
            },
        )
        assert applied["applied"] is True
        assert int(applied["commit_seq"]) > int(dry_run["commit_seq"])

        replay = await client.call(
            "session.repair.codex_launch_visibility.v2",
            {
                "session_id": session_id,
                "dry_run": False,
                "expected_fingerprint": dry_run["expected_fingerprint"],
            },
        )
        assert replay["applied"] is False
        assert {"launch_actor_already_set", "launch_surface_already_set", "not_policy_hidden"} <= set(
            replay["refusals"]
        )
        assert replay["commit_seq"] == applied["commit_seq"]
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        catalog = db.get(LiveSessionCatalog, session_id)
        card = db.get(LiveTimelineCard, session_id)
        thread = db.query(LiveSessionThread).filter_by(session_id=session_id).one()
        assert (catalog.launch_actor, catalog.launch_surface, catalog.hidden_from_default_timeline) == (
            "human_shell",
            "terminal",
            0,
        )
        assert (card.launch_actor, card.launch_surface, card.hidden_from_default_timeline) == (
            "human_shell",
            "terminal",
            0,
        )
        assert thread.hidden_from_default_timeline == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_codex_visibility_repair_refuses_user_hidden_row_and_apply_without_receipt(daemon_paths):
    database_path, socket_path = daemon_paths
    session_id = _seed_eligible_session(database_path, user_hidden=True)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        refusal = await client.call(
            "session.repair.codex_launch_visibility.v2",
            {"session_id": session_id, "dry_run": True, "expected_fingerprint": None},
        )
        assert refusal["eligible"] is False
        assert {"user_hidden", "timeline_card_user_hidden"} <= set(refusal["refusals"])

        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "session.repair.codex_launch_visibility.v2",
                {"session_id": session_id, "dry_run": False, "expected_fingerprint": None},
            )
        assert exc_info.value.code == "invalid_request"
        assert "fingerprint from dry-run" in str(exc_info.value)
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        catalog = db.get(LiveSessionCatalog, session_id)
        assert catalog.launch_actor is None
        assert catalog.hidden_from_default_timeline == 1
        assert catalog.user_hidden_from_timeline == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_codex_visibility_repair_refuses_when_fact_changes_after_dry_run(daemon_paths):
    database_path, socket_path = daemon_paths
    session_id = _seed_eligible_session(database_path)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        dry_run = await client.call(
            "session.repair.codex_launch_visibility.v2",
            {"session_id": session_id, "dry_run": True, "expected_fingerprint": None},
        )
        assert dry_run["eligible"] is True
        await client.call(
            "session.preferences.update.v2",
            {
                "session_id": session_id,
                "user_state": None,
                "loop_mode": None,
                "notification_muted": None,
                "user_hidden_from_timeline": True,
                "last_read_at": None,
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )

        stale_apply = await client.call(
            "session.repair.codex_launch_visibility.v2",
            {
                "session_id": session_id,
                "dry_run": False,
                "expected_fingerprint": dry_run["expected_fingerprint"],
            },
        )
        assert stale_apply["applied"] is False
        assert {"user_hidden", "timeline_card_user_hidden"} <= set(stale_apply["refusals"])
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        catalog = db.get(LiveSessionCatalog, session_id)
        assert catalog.launch_actor is None
        assert catalog.launch_surface is None
        assert catalog.hidden_from_default_timeline == 1
        assert catalog.user_hidden_from_timeline == 1
    engine.dispose()
