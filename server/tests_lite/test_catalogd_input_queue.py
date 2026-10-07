from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.models import FactHead
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputAttachment
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.live_session_inputs import upsert_live_input_receipt


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-input-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_queue(engine, *, client_request_id="queued-1"):
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    adapter_connection_id = f"connection-{session_id}"
    lease_generation = f"lease-{session_id}"
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                primary_thread_id=str(thread_id),
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveSessionThread(
                id=str(thread_id),
                session_id=str(session_id),
                provider="codex",
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
                provider="codex",
                host_id="cinder",
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
        db.add(
            LiveSessionConnection(
                run_id=str(run_id),
                control_plane="codex_bridge",
                acquisition_kind="spawned_control",
                adapter_connection_id=adapter_connection_id,
                lease_generation=lease_generation,
                state="attached",
                device_id="cinder",
                can_send_input=1,
                acquired_at=now,
                last_health_at=now,
            )
        )
        db.add(
            LiveRuntimeState(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                phase="thinking",
                phase_source="test",
                timeline_anchor_at=now,
                runtime_version=1,
                updated_at=now,
            )
        )
        receipt = upsert_live_input_receipt(
            db,
            owner_id=7,
            session_id=session_id,
            provider="codex",
            text="continue the migration",
            intent="auto",
            status="queued",
            client_request_id=client_request_id,
            now=now,
        )
        db.commit()
        activity = {
            "authority_class": "provider_runtime",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run_id),
            "kind": "idle",
            "raw_kind": "idle",
            "source": "provider_runtime",
            "observed_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=2)).isoformat(),
        }
        control = {
            "authority_class": "provider_control",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run_id),
            "connection_id": adapter_connection_id,
            "lease_generation": lease_generation,
            "granted_operations": ["send_input"],
            "state": "attached",
            "lease_ttl_ms": 120_000,
            "source": "provider_control",
            "observed_at": now.isoformat(),
        }
        reduce_fact_batch(
            db.connection(),
            [
                ReducerFact(
                    family="activity",
                    subject_key=f"run:{run_id}",
                    source="provider_runtime",
                    source_epoch=str(run_id),
                    source_seq=1,
                    dedupe_key="a" * 64,
                    evidence_hash=canonical_evidence_hash(activity),
                    value=activity,
                    observed_at=now,
                    session_id=str(session_id),
                    valid_until=now + timedelta(minutes=2),
                ),
                ReducerFact(
                    family="control",
                    subject_key=f"connection:{adapter_connection_id}:{lease_generation}",
                    source="provider_control",
                    source_epoch=lease_generation,
                    source_seq=1,
                    dedupe_key="b" * 64,
                    evidence_hash=canonical_evidence_hash(control),
                    value=control,
                    observed_at=now,
                    session_id=str(session_id),
                    valid_until=now + timedelta(minutes=2),
                ),
            ],
            received_at=now,
        )
        db.commit()
        return session_id, str(receipt.id)


def _replace_activity_head(engine, session_id, *, kind: str):
    observed_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    with Session(engine) as db:
        run = db.query(LiveSessionRun).one()
        value = {
            "authority_class": "provider_runtime",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run.id),
            "kind": kind,
            "raw_kind": kind,
            "source": "provider_runtime",
            "observed_at": observed_at.isoformat(),
            "valid_until": (observed_at + timedelta(minutes=2)).isoformat(),
        }
        reduce_fact_batch(
            db.connection(),
            [
                ReducerFact(
                    family="activity",
                    subject_key=f"run:{run.id}",
                    source="provider_runtime",
                    source_epoch=str(run.id),
                    source_seq=2,
                    dedupe_key=canonical_evidence_hash({"kind": kind, "seq": 2}),
                    evidence_hash=canonical_evidence_hash(value),
                    value=value,
                    observed_at=observed_at,
                    session_id=str(session_id),
                    valid_until=observed_at + timedelta(minutes=2),
                )
            ],
            received_at=observed_at,
        )
        db.commit()


def _replace_expired_activity_head(engine, session_id, *, kind: str, age: timedelta = timedelta(minutes=10)):
    """An observation the Machine Agent made `age` ago and has not restated since.

    The head's lease is anchored on when the host received it, so a hook provider
    that went quiet after its last transition reads as `unknown` once the short
    activity lease lapses, however fresh the control lease still is.
    """

    observed_at = datetime.now(UTC).replace(microsecond=0) - age
    with Session(engine) as db:
        run = db.query(LiveSessionRun).one()
        value = {
            "authority_class": "provider_runtime",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run.id),
            "kind": kind,
            "raw_kind": kind,
            "source": "provider_runtime",
            "observed_at": observed_at.isoformat(),
            "valid_until": (observed_at + timedelta(seconds=15)).isoformat(),
        }
        reduce_fact_batch(
            db.connection(),
            [
                ReducerFact(
                    family="activity",
                    subject_key=f"run:{run.id}",
                    source="provider_runtime",
                    source_epoch=str(run.id),
                    source_seq=3,
                    dedupe_key=canonical_evidence_hash({"kind": kind, "seq": 3, "expired": True}),
                    evidence_hash=canonical_evidence_hash(value),
                    value=value,
                    observed_at=observed_at,
                    session_id=str(session_id),
                    valid_until=observed_at + timedelta(seconds=15),
                )
            ],
            received_at=observed_at,
        )
        db.commit()


def _replace_control_grants(engine, session_id, *, granted_operations: list[str]):
    observed_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    with Session(engine) as db:
        run = db.query(LiveSessionRun).one()
        connection = db.query(LiveSessionConnection).one()
        value = {
            "authority_class": "provider_control",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run.id),
            "connection_id": connection.adapter_connection_id,
            "lease_generation": connection.lease_generation,
            "granted_operations": granted_operations,
            "state": "attached",
            "lease_ttl_ms": 120_000,
            "source": "provider_control",
            "observed_at": observed_at.isoformat(),
        }
        reduce_fact_batch(
            db.connection(),
            [
                ReducerFact(
                    family="control",
                    subject_key=f"connection:{connection.adapter_connection_id}:{connection.lease_generation}",
                    source="provider_control",
                    source_epoch=str(connection.lease_generation),
                    source_seq=2,
                    dedupe_key=canonical_evidence_hash({"grants": granted_operations, "seq": 2}),
                    evidence_hash=canonical_evidence_hash(value),
                    value=value,
                    observed_at=observed_at,
                    session_id=str(session_id),
                    valid_until=observed_at + timedelta(minutes=2),
                )
            ],
            received_at=observed_at,
        )
        db.commit()


@pytest.mark.asyncio
async def test_catalogd_claims_and_finishes_queued_input_exactly_once(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, receipt_id = _seed_queue(engine)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        queued = await client.call("session.input.queued.list.v2", {"limit": 100})
        assert queued["session_ids"] == [str(session_id)]
        params = {"session_id": str(session_id), "delivery_request_id": "delivery-1"}
        claimed = await client.call("session.input.claim.v2", params)
        assert claimed["claimed"] is True, claimed
        assert claimed["receipt"]["id"] == receipt_id
        assert claimed["session"]["device_id"] == "cinder"
        replay = await client.call("session.input.claim.v2", params)
        assert replay["exact_replay"] is True
        finished = await client.call(
            "session.input.finish.v2",
            {
                "receipt_id": receipt_id,
                "delivery_request_id": "delivery-1",
                "status": "delivered",
                "error": None,
            },
        )
        assert finished["changed"] is True
        finish_replay = await client.call(
            "session.input.finish.v2",
            {
                "receipt_id": receipt_id,
                "delivery_request_id": "delivery-1",
                "status": "delivered",
                "error": None,
            },
        )
        assert finish_replay["changed"] is False
        upserted = await client.call(
            "session.input.receipt.upsert.v2",
            {
                "receipt": {
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "provider": "codex",
                    "text": "a second queued input",
                    "intent": "queue",
                    "status": "queued",
                    "client_request_id": "queued-2",
                    "device_id": "cinder",
                    "thread_id": None,
                    "archive_session_input_id": None,
                    "control_command_id": None,
                    "delivery_request_id": None,
                    "enqueue_archive_projection": False,
                    "error": None,
                    "expires_at": None,
                }
            },
        )
        second_id = upserted["receipt"]["id"]
        read = await client.call(
            "session.input.receipt.read.v2",
            {
                "owner_id": 7,
                "session_id": str(session_id),
                "client_request_id": "queued-2",
            },
        )
        assert read["receipt"]["id"] == second_id
        recent = await client.call("session.input.recent.list.v2", {"session_id": str(session_id)})
        assert recent["queued_count"] == 1
        assert {receipt["id"] for receipt in recent["receipts"]} == {receipt_id, second_id}
        assert recent["receipts"] == sorted(
            recent["receipts"],
            key=lambda receipt: (receipt["created_at"], receipt["id"]),
        )
        cancelled = await client.call(
            "session.input.cancel.v2",
            {"session_id": str(session_id), "receipt_id": second_id},
        )
        assert cancelled["cancelled"] is True
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        assert db.get(LiveSessionInputReceipt, receipt_id).status == "delivered"
        assert db.query(LiveArchiveOutbox).count() == 0
    engine.dispose()


@pytest.mark.parametrize(
    ("turn_age_minutes", "run_reporting", "expected_fresh"),
    [
        (10, False, True),
        # A long tool call: the turn entered `active` twenty minutes ago and the
        # machine still reports the run, so it is current work, not stale.
        (20, True, True),
        (20, False, False),
    ],
)
def test_recent_input_list_keeps_nonterminal_console_receipt_past_delivered_window(
    tmp_path,
    turn_age_minutes,
    run_reporting,
    expected_fresh,
):
    engine = create_catalog_engine(tmp_path / "recent-inputs.db")
    initialize_catalog_schema(engine)
    session_id, active_receipt_id = _seed_queue(engine, client_request_id="console-active")
    stale_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=turn_age_minutes)
    try:
        with Session(engine) as db:
            active_receipt = db.get(LiveSessionInputReceipt, active_receipt_id)
            assert active_receipt is not None
            active_receipt.status = "delivered"
            active_receipt.updated_at = stale_at
            catalog = db.get(LiveSessionCatalog, str(session_id))
            assert catalog is not None
            thread_id = catalog.primary_thread_id
            run = db.query(LiveSessionRun).filter_by(thread_id=thread_id).one()
            if not run_reporting:
                # The seeded run's only evidence is its control attachment.
                connection = db.query(LiveSessionConnection).filter_by(run_id=run.id).one()
                connection.last_health_at = stale_at
            db.add(
                LiveConsoleTurn(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    thread_id=thread_id,
                    receipt_id=active_receipt_id,
                    run_id=run.id,
                    state="active",
                    provider="codex",
                    device_id="cinder",
                    cwd="/workspace/longhouse",
                    created_at=stale_at,
                    updated_at=stale_at,
                )
            )

            terminal_receipt = upsert_live_input_receipt(
                db,
                owner_id=7,
                session_id=session_id,
                provider="codex",
                text="completed old Console turn",
                intent="auto",
                status="delivered",
                client_request_id="console-completed-old",
                now=stale_at,
            )
            # In the transcript, so the recent window alone decides.
            terminal_receipt.durable_event_id = "event-completed-old"
            db.add(
                LiveConsoleTurn(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    thread_id=thread_id,
                    receipt_id=str(terminal_receipt.id),
                    run_id=None,
                    state="completed",
                    provider="codex",
                    device_id="cinder",
                    cwd="/workspace/longhouse",
                    created_at=stale_at,
                    updated_at=stale_at,
                    terminal_at=stale_at,
                )
            )
            db.commit()

        recent = CatalogStore(engine).list_recent_input_receipts(session_id=str(session_id))

        assert [receipt["id"] for receipt in recent["receipts"]] == [active_receipt_id]
        assert recent["receipts"][0]["turn"]["state"] == "active"
        assert recent["receipts"][0]["turn"]["is_fresh"] is expected_fresh
        assert recent["queued_count"] == 0
    finally:
        engine.dispose()


def test_recent_input_list_keeps_delivered_user_sends_missing_from_the_transcript(tmp_path):
    engine = create_catalog_engine(tmp_path / "unlinked-inputs.db")
    initialize_catalog_schema(engine)
    session_id, queued_receipt_id = _seed_queue(engine, client_request_id="still-queued")
    old = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)
    try:
        with Session(engine) as db:
            catalog = db.get(LiveSessionCatalog, str(session_id))
            assert catalog is not None

            def receipt(client_request_id, *, intent="auto", minutes=0, durable_event_id=None):
                row = upsert_live_input_receipt(
                    db,
                    owner_id=7,
                    session_id=session_id,
                    provider="claude",
                    text=client_request_id,
                    intent=intent,
                    status="delivered",
                    client_request_id=client_request_id,
                    now=old + timedelta(minutes=minutes),
                )
                row.durable_event_id = durable_event_id
                row.created_at = old + timedelta(minutes=minutes)
                return str(row.id)

            def turn(receipt_id, *, origin, state="completed"):
                db.add(
                    LiveConsoleTurn(
                        id=str(uuid4()),
                        session_id=str(session_id),
                        thread_id=catalog.primary_thread_id,
                        receipt_id=receipt_id,
                        run_id=None,
                        state=state,
                        origin=origin,
                        provider="claude",
                        device_id="cinder",
                        cwd="/workspace/longhouse",
                        created_at=old,
                        updated_at=old,
                        terminal_at=old,
                    )
                )

            lost_send = receipt("ios-lost-send", minutes=1)
            turn(lost_send, origin="user", state="failed")
            steer = receipt("ios-steer", intent="steer", minutes=2)
            transcribed = receipt("web-transcribed", minutes=3, durable_event_id="event-1")
            turn(transcribed, origin="user")
            wake = receipt("wake:task:1", minutes=4)
            turn(wake, origin="wake")
            db.commit()

        recent = CatalogStore(engine).list_recent_input_receipts(session_id=str(session_id))

        # Oldest first; the queued one was seeded now, after all of these.
        assert [row["id"] for row in recent["receipts"]] == [lost_send, steer, queued_receipt_id]
        assert recent["receipts"][0]["turn"]["state"] == "failed"
        assert recent["receipts"][1]["turn"] is None
        assert transcribed not in {row["id"] for row in recent["receipts"]}
        assert wake not in {row["id"] for row in recent["receipts"]}
    finally:
        engine.dispose()


@pytest.mark.parametrize(("receipt_age_minutes", "visible"), [(10, True), (20, False)])
def test_recent_input_list_keeps_cancelled_console_receipt_for_remote_clients(
    tmp_path,
    receipt_age_minutes,
    visible,
):
    engine = create_catalog_engine(tmp_path / "cancelled-inputs.db")
    initialize_catalog_schema(engine)
    session_id, receipt_id = _seed_queue(engine, client_request_id="console-cancelled")
    cancelled_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=receipt_age_minutes)
    try:
        with Session(engine) as db:
            receipt = db.get(LiveSessionInputReceipt, receipt_id)
            assert receipt is not None
            receipt.status = "cancelled"
            receipt.updated_at = cancelled_at
            catalog = db.get(LiveSessionCatalog, str(session_id))
            assert catalog is not None
            db.add(
                LiveConsoleTurn(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    thread_id=catalog.primary_thread_id,
                    receipt_id=receipt_id,
                    run_id=None,
                    state="cancelled",
                    provider="codex",
                    device_id="cinder",
                    cwd="/workspace/longhouse",
                    created_at=cancelled_at,
                    updated_at=cancelled_at,
                    terminal_at=cancelled_at,
                )
            )
            db.commit()

        recent = CatalogStore(engine).list_recent_input_receipts(session_id=str(session_id))
        if visible:
            assert [row["id"] for row in recent["receipts"]] == [receipt_id]
            assert recent["receipts"][0]["status"] == "cancelled"
            assert recent["receipts"][0]["turn"]["state"] == "cancelled"
        else:
            assert recent["receipts"] == []
        assert recent["queued_count"] == 0
    finally:
        engine.dispose()


def test_recent_input_list_exposes_only_attachment_display_metadata(tmp_path):
    engine = create_catalog_engine(tmp_path / "recent-input-attachments.db")
    initialize_catalog_schema(engine)
    session_id, receipt_id = _seed_queue(engine)
    now = datetime.now(UTC)
    try:
        with Session(engine) as db:
            db.add(
                LiveSessionInputAttachment(
                    id=str(uuid4()),
                    input_receipt_id=receipt_id,
                    owner_id=7,
                    session_id=str(session_id),
                    mime_type="image/png",
                    byte_size=123,
                    sha256="a" * 64,
                    blob_path=str(tmp_path / "private-photo.bin"),
                    original_filename="photo.png",
                    original_byte_size=456,
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            db.commit()

        store = CatalogStore(engine)
        recent = store.list_recent_input_receipts(session_id=str(session_id))
        attachments = recent["receipts"][0]["attachments"]
        assert attachments == [{"filename": "photo.png", "mime_type": "image/png", "byte_size": 123}]

        exact = store.read_input_receipt(
            owner_id=7,
            session_id=str(session_id),
            client_request_id="queued-1",
        )
        assert exact["receipt"]["attachments"] == attachments
    finally:
        engine.dispose()


@pytest.fixture
def mutates_queue_db(tmp_path):
    engine = create_catalog_engine(tmp_path / "queue-denial.db")
    initialize_catalog_schema(engine)
    session_id, _receipt_id = _seed_queue(engine)
    yield engine, session_id
    engine.dispose()


def _delete_control_heads(engine):
    with Session(engine) as db:
        db.query(FactHead).filter(FactHead.family == "control").delete(synchronize_session=False)
        db.commit()


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (
            lambda engine, session_id: _replace_activity_head(engine, session_id, kind="running"),
            "activity_not_drainable",
        ),
        (lambda engine, _session_id: _delete_control_heads(engine), "control_unavailable"),
        (
            lambda engine, session_id: _replace_control_grants(engine, session_id, granted_operations=[]),
            "control_unavailable",
        ),
    ],
)
def test_catalogd_queue_claim_uses_canonical_activity_and_control(mutates_queue_db, mutate, expected_reason):
    engine, session_id = mutates_queue_db
    mutate(engine, session_id)

    result = CatalogStore(engine).claim_queued_input(
        session_id=str(session_id),
        delivery_request_id=f"denied-{expected_reason}",
    )

    assert result["claimed"] is False
    assert result["reason"] == expected_reason
    assert result["commit_seq"].isdigit()


def test_catalogd_blocked_activity_can_drain_directed_input(tmp_path):
    engine = create_catalog_engine(tmp_path / "blocked-directed-input.db")
    initialize_catalog_schema(engine)
    session_id, _receipt_id = _seed_queue(engine, client_request_id="directed-input-42")
    _replace_activity_head(engine, session_id, kind="blocked")

    result = CatalogStore(engine).claim_queued_input(
        session_id=str(session_id),
        delivery_request_id="blocked-directed-input",
    )

    assert result["claimed"] is True
    engine.dispose()


def test_catalogd_idle_observation_that_lapsed_still_drains_while_control_is_live(tmp_path):
    """The phone sent "Stop it all" to an idle Claude session and the composer said
    "1 message queued -- will send at next turn boundary" for two minutes.

    The session's last hook was `idle` ten minutes before the send. A hook provider
    is silent between transitions, so that observation's 15 s activity lease lapsed
    and the served activity state read `unknown` while the headline, which already
    knows a live control lease keeps a Helm idle, said "Idle". The drain asked the
    narrower question and waited for a turn boundary that was never coming.
    """

    engine = create_catalog_engine(tmp_path / "lapsed-idle.db")
    initialize_catalog_schema(engine)
    session_id, receipt_id = _seed_queue(engine, client_request_id="ios-FA93E7FD")
    _replace_expired_activity_head(engine, session_id, kind="idle")
    store = CatalogStore(engine)

    assert store.read_session_activity(session_id=str(session_id))["activity_state"] == "quiescent"
    result = store.claim_queued_input(session_id=str(session_id), delivery_request_id="lapsed-idle")

    assert result["claimed"] is True
    assert result["receipt"]["id"] == receipt_id
    engine.dispose()


@pytest.mark.parametrize(("kind", "held_as"), [("thinking", "thinking"), ("running", "executing")])
def test_catalogd_running_observation_that_lapsed_is_still_not_a_turn_boundary(tmp_path, kind, held_as):
    """The inverse holds: a *running* phase that went quiet proves nothing about the
    turn having ended. A Helm session's live control lease holds it as running (the
    owning process is alive and its hooks report every way out of the turn), so SEND
    keeps waiting instead of dispatching into it."""

    engine = create_catalog_engine(tmp_path / f"lapsed-{kind}.db")
    initialize_catalog_schema(engine)
    session_id, _receipt_id = _seed_queue(engine)
    _replace_expired_activity_head(engine, session_id, kind=kind)
    store = CatalogStore(engine)

    assert store.read_session_activity(session_id=str(session_id))["activity_state"] == held_as
    result = store.claim_queued_input(session_id=str(session_id), delivery_request_id=f"lapsed-{kind}")

    assert result["claimed"] is False
    assert result["reason"] == "activity_not_drainable"
    engine.dispose()


def test_catalogd_lapsed_idle_does_not_drain_once_control_is_gone(tmp_path):
    engine = create_catalog_engine(tmp_path / "lapsed-idle-no-control.db")
    initialize_catalog_schema(engine)
    session_id, _receipt_id = _seed_queue(engine)
    _replace_expired_activity_head(engine, session_id, kind="idle")
    _delete_control_heads(engine)
    store = CatalogStore(engine)

    assert store.read_session_activity(session_id=str(session_id))["activity_state"] == "unknown"
    result = store.claim_queued_input(session_id=str(session_id), delivery_request_id="lapsed-no-control")

    assert result["claimed"] is False
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_attachment_metadata_is_receipt_scoped_and_bounded(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, receipt_id = _seed_queue(engine)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    attachment_id = str(uuid4())
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    try:
        created = await client.call(
            "session.input.attachment.create.v2",
            {
                "attachment": {
                    "id": attachment_id,
                    "input_receipt_id": receipt_id,
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "mime_type": "image/png",
                    "byte_size": 67,
                    "sha256": "a" * 64,
                    "blob_path": f"{session_id}/{attachment_id}.bin",
                    "original_filename": "image.png",
                    "original_byte_size": 67,
                    "expires_at": expires_at.isoformat(),
                },
                "allow_unbound": False,
            },
        )
        assert created["created"] is True
        attachment_read = await client.call(
            "session.input.attachment.read.v2",
            {
                "owner_id": 7,
                "session_id": str(session_id),
                "input_receipt_id": receipt_id,
                "attachment_id": attachment_id,
            },
        )
        assert attachment_read["found"] is True
        assert attachment_read["attachment"]["sha256"] == "a" * 64
        expired_id = str(uuid4())
        expired = await client.call(
            "session.input.attachment.create.v2",
            {
                "attachment": {
                    "id": expired_id,
                    "input_receipt_id": receipt_id,
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "mime_type": "image/png",
                    "byte_size": 67,
                    "sha256": "b" * 64,
                    "blob_path": f"{session_id}/{expired_id}.bin",
                    "original_filename": "expired.png",
                    "original_byte_size": 67,
                    "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                },
                "allow_unbound": False,
            },
        )
        assert expired["created"] is True
        expired_read = await client.call(
            "session.input.attachment.read.v2",
            {
                "owner_id": 7,
                "session_id": str(session_id),
                "input_receipt_id": receipt_id,
                "attachment_id": expired_id,
            },
        )
        assert expired_read["found"] is False
        replacement_id = str(uuid4())
        replacement = await client.call(
            "session.input.attachment.create.v2",
            {
                "attachment": {
                    "id": replacement_id,
                    "input_receipt_id": receipt_id,
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "mime_type": "image/png",
                    "byte_size": 67,
                    "sha256": "c" * 64,
                    "blob_path": f"{session_id}/{replacement_id}.bin",
                    "original_filename": "replacement.png",
                    "original_byte_size": 67,
                    "expires_at": expires_at.isoformat(),
                },
                "allow_unbound": False,
            },
        )
        assert replacement["created"] is True
        assert replacement["pruned_blob_paths"] == [f"{session_id}/{expired_id}.bin"]
        missing_receipt = await client.call(
            "session.input.attachment.create.v2",
            {
                "attachment": {
                    "id": str(uuid4()),
                    "input_receipt_id": str(uuid4()),
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "mime_type": "image/png",
                    "byte_size": 67,
                    "sha256": "b" * 64,
                    "blob_path": f"{session_id}/missing.bin",
                    "original_filename": "missing.png",
                    "original_byte_size": 67,
                    "expires_at": expires_at.isoformat(),
                },
                "allow_unbound": False,
            },
        )
        assert missing_receipt["attachment"] is None
        assert missing_receipt["reason"] == "input_receipt_not_found"

        assert created["attachment"]["input_receipt_id"] == receipt_id

        lookup = {
            "owner_id": 7,
            "session_id": str(session_id),
            "input_receipt_id": receipt_id,
            "attachment_id": attachment_id,
        }
        found = await client.call("session.input.attachment.read.v2", lookup)
        assert found["found"] is True
        assert found["attachment"]["sha256"] == "a" * 64
        wrong_owner = await client.call("session.input.attachment.read.v2", {**lookup, "owner_id": 8})
        assert wrong_owner["found"] is False
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        assert db.get(LiveSessionInputAttachment, attachment_id).input_receipt_id == receipt_id
    engine.dispose()


def test_queued_console_receipt_stays_fresh_behind_a_reporting_long_turn(tmp_path):
    engine = create_catalog_engine(tmp_path / "queued-behind-long-turn.db")
    initialize_catalog_schema(engine)
    session_id, active_receipt_id = _seed_queue(engine, client_request_id="console-active")
    long_ago = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=40)
    try:
        with Session(engine) as db:
            catalog = db.get(LiveSessionCatalog, str(session_id))
            assert catalog is not None
            thread_id = catalog.primary_thread_id
            run = db.query(LiveSessionRun).filter_by(thread_id=thread_id).one()
            # The machine stopped stamping the attachment but keeps asserting
            # the run's status while its tool call runs.
            db.query(LiveSessionConnection).filter_by(run_id=run.id).one().last_health_at = long_ago
            runtime = db.query(LiveRuntimeState).filter_by(session_id=session_id).one()
            runtime.run_id = run.id
            runtime.last_asserted_at = datetime.now(UTC) - timedelta(seconds=5)
            # Only the assertion is recent: the phase itself was observed long ago.
            runtime.updated_at = long_ago
            active_receipt = db.get(LiveSessionInputReceipt, active_receipt_id)
            assert active_receipt is not None
            active_receipt.status = "delivered"
            db.add(
                LiveConsoleTurn(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    thread_id=thread_id,
                    receipt_id=active_receipt_id,
                    run_id=run.id,
                    state="active",
                    provider="codex",
                    device_id="cinder",
                    cwd="/workspace/longhouse",
                    created_at=long_ago,
                    updated_at=long_ago,
                )
            )
            queued_receipt = upsert_live_input_receipt(
                db,
                owner_id=7,
                session_id=session_id,
                provider="codex",
                text="after the long call",
                intent="auto",
                status="queued",
                client_request_id="console-queued",
                now=long_ago + timedelta(minutes=1),
            )
            db.add(
                LiveConsoleTurn(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    thread_id=thread_id,
                    receipt_id=str(queued_receipt.id),
                    run_id=None,
                    state="queued",
                    provider="codex",
                    device_id="cinder",
                    cwd="/workspace/longhouse",
                    created_at=long_ago + timedelta(minutes=1),
                    updated_at=long_ago + timedelta(minutes=1),
                )
            )
            db.commit()

        store = CatalogStore(engine)
        recent = store.list_recent_input_receipts(session_id=str(session_id))
        by_request = {receipt["client_request_id"]: receipt["turn"] for receipt in recent["receipts"]}
        assert by_request["console-active"]["is_fresh"] is True
        assert by_request["console-queued"]["state"] == "queued"
        assert by_request["console-queued"]["is_fresh"] is True
        single = store.read_input_receipt(owner_id=7, session_id=str(session_id), client_request_id="console-queued")
        assert single["receipt"]["turn"]["is_fresh"] is True

        # Once the run's status assertions stop, both read as unknown again.
        with Session(engine) as db:
            runtime = db.query(LiveRuntimeState).filter_by(session_id=session_id).one()
            runtime.last_asserted_at = long_ago
            runtime.updated_at = long_ago
            db.commit()
        recent = store.list_recent_input_receipts(session_id=str(session_id))
        assert all(receipt["turn"]["is_fresh"] is False for receipt in recent["receipts"])
    finally:
        engine.dispose()


def test_unsettled_turn_of_an_ended_run_stays_stale_beside_a_reporting_run(tmp_path):
    engine = create_catalog_engine(tmp_path / "orphan-turn.db")
    initialize_catalog_schema(engine)
    session_id, old_receipt_id = _seed_queue(engine, client_request_id="console-orphan")
    long_ago = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=40)
    try:
        with Session(engine) as db:
            catalog = db.get(LiveSessionCatalog, str(session_id))
            assert catalog is not None
            thread_id = catalog.primary_thread_id
            # The Console run was retired without its turn ever settling, and a
            # different run on the same thread is reporting now.
            old_run = db.query(LiveSessionRun).filter_by(thread_id=thread_id).one()
            old_run.ended_at = long_ago
            db.query(LiveSessionConnection).filter_by(run_id=old_run.id).one().last_health_at = long_ago
            other_run_id = str(uuid4())
            db.add(
                LiveSessionRun(
                    id=other_run_id,
                    thread_id=thread_id,
                    provider="codex",
                    host_id="cinder",
                    launch_origin="longhouse_spawned",
                    started_at=long_ago,
                )
            )
            runtime = db.query(LiveRuntimeState).filter_by(session_id=session_id).one()
            runtime.run_id = other_run_id
            runtime.last_asserted_at = datetime.now(UTC) - timedelta(seconds=5)
            db.get(LiveSessionInputReceipt, old_receipt_id).status = "delivered"
            queued_receipt = upsert_live_input_receipt(
                db,
                owner_id=7,
                session_id=session_id,
                provider="codex",
                text="queued behind the orphan",
                intent="auto",
                status="queued",
                client_request_id="console-queued",
                now=long_ago,
            )
            for receipt_id, run_id, state in (
                (old_receipt_id, old_run.id, "active"),
                (str(queued_receipt.id), None, "queued"),
            ):
                db.add(
                    LiveConsoleTurn(
                        id=str(uuid4()),
                        session_id=str(session_id),
                        thread_id=thread_id,
                        receipt_id=receipt_id,
                        run_id=run_id,
                        state=state,
                        provider="codex",
                        device_id="cinder",
                        cwd="/workspace/longhouse",
                        created_at=long_ago,
                        updated_at=long_ago,
                    )
                )
            db.commit()

        recent = CatalogStore(engine).list_recent_input_receipts(session_id=str(session_id))
        by_request = {receipt["client_request_id"]: receipt["turn"] for receipt in recent["receipts"]}
        assert by_request["console-orphan"]["is_fresh"] is False
        assert by_request["console-queued"]["is_fresh"] is False
    finally:
        engine.dispose()
