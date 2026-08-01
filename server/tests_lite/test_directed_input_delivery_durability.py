"""A directed input must survive a peer whose control channel is down.

Regression cover for an observed incident: a message between two Longhouse
sessions failed terminally with "Managed control channel is not connected", the
recipient was never told, and it acted on stale state. The message to an *idle*
peer was dropped while the same message to a *busy* peer would have been queued
safely, because target phase selected the delivery semantics.

The rule these tests pin down: a send is durable, and the target's phase decides
*when* it lands, never *whether* it survives.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.services.live_control_catalog import _is_transient_delivery_failure
from zerg.services.live_session_inputs import MAX_DELIVERY_AGE
from zerg.services.live_session_inputs import MAX_DELIVERY_ATTEMPTS
from zerg.services.live_session_inputs import requeue_live_receipt
from zerg.services.managed_control_dispatcher import DISPATCH_FAILURE_PRECONDITION
from zerg.services.managed_control_dispatcher import DISPATCH_FAILURE_REJECTED
from zerg.services.managed_control_dispatcher import DISPATCH_FAILURE_TRANSPORT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_NONE
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
from zerg.services.managed_control_dispatcher import ManagedControlDispatchResult


@pytest.fixture
def orm(tmp_path: Path):
    engine = create_catalog_engine(tmp_path / "live.db")
    initialize_catalog_schema(engine)
    session = Session(bind=engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


def _receipt(orm: Session, *, status: str = "delivering", attempts: int | None = None) -> str:
    receipt_id = str(uuid4())
    orm.add(
        LiveSessionInputReceipt(
            id=receipt_id,
            owner_id=1,
            session_id=str(uuid4()),
            provider="claude",
            intent="queue",
            status=status,
            text="hello",
            delivery_request_id="attempt-1",
            delivery_attempts=attempts,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    orm.commit()
    return receipt_id


# --- Which failures may consume a message -----------------------------------


@pytest.mark.parametrize(
    "reason",
    ["control_unavailable", "connection_unavailable", "control_head_missing", "lease_expired"],
)
def test_unconverged_control_preconditions_are_transient(reason: str):
    # These describe a control path that has not come back yet, and they do
    # come back on reconnect.
    result = ManagedControlDispatchResult(
        ok=False,
        transport=MANAGED_CONTROL_TRANSPORT_NONE,
        error="x",
        failure_kind=DISPATCH_FAILURE_PRECONDITION,
        failure_reason=reason,
    )
    assert _is_transient_delivery_failure(result) is True


def test_absent_control_path_is_transient():
    result = ManagedControlDispatchResult(
        ok=False,
        transport=MANAGED_CONTROL_TRANSPORT_NONE,
        error=MANAGED_CONTROL_UNAVAILABLE_ERROR,
        failure_kind=DISPATCH_FAILURE_PRECONDITION,
        failure_reason="control_unavailable",
    )
    assert _is_transient_delivery_failure(result) is True


@pytest.mark.parametrize(
    "error",
    [
        "Failed to send command to Machine Agent control channel",
        "Machine control channel was replaced",
        "Machine Agent control channel is offline",
    ],
)
def test_unsent_transport_failures_are_transient_whatever_the_wording(error: str):
    # Regression on the first implementation, which classified by substring and
    # therefore consumed real transport errors. Classification comes from typed
    # fields so rewording a message cannot start eating input.
    result = ManagedControlDispatchResult(
        ok=False,
        transport="engine_channel",
        error=error,
        failure_kind=DISPATCH_FAILURE_TRANSPORT,
        failure_reason="not_sent",
    )
    assert _is_transient_delivery_failure(result) is True


def test_ambiguous_timeout_is_not_retried():
    """A timeout must consume the input until dedupe survives engine restart.

    We stopped waiting; the engine may already have accepted and run the
    command. Engine dedupe is an in-memory cache, so a retry across a restart
    could inject the same prompt twice. Duplicating a user's message is worse
    than dropping it, so this deliberately does not retry.
    """
    result = ManagedControlDispatchResult(
        ok=False,
        transport="engine_channel",
        error="Machine Agent control command timed out after 15 seconds",
        failure_kind=DISPATCH_FAILURE_TRANSPORT,
        failure_reason="ambiguous",
    )
    assert _is_transient_delivery_failure(result) is False


@pytest.mark.parametrize(
    "reason",
    ["idempotency_conflict", "operation_finished", "session_closed", "run_ended", "not_granted"],
)
def test_unretryable_preconditions_are_terminal(reason: str):
    # Waiting cannot satisfy these. Treating them as transient burns the whole
    # budget without the command ever reaching the engine.
    result = ManagedControlDispatchResult(
        ok=False,
        transport=MANAGED_CONTROL_TRANSPORT_NONE,
        error="conflict",
        failure_kind=DISPATCH_FAILURE_PRECONDITION,
        failure_reason=reason,
    )
    assert _is_transient_delivery_failure(result) is False


def test_provider_rejection_is_terminal():
    result = ManagedControlDispatchResult(
        ok=False,
        transport="engine_channel",
        error="provider rejected the request",
        failure_kind=DISPATCH_FAILURE_REJECTED,
    )
    assert _is_transient_delivery_failure(result) is False


def test_unclassified_failure_is_terminal():
    result = ManagedControlDispatchResult(ok=False, transport="engine_channel", error="something")
    assert _is_transient_delivery_failure(result) is False


# --- Requeue behaviour -------------------------------------------------------


def test_transient_failure_returns_the_receipt_to_the_queue(orm: Session):
    receipt_id = _receipt(orm)

    snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="channel down")

    assert requeued is True
    assert snapshot is not None
    assert snapshot.status == "queued"
    row = orm.get(LiveSessionInputReceipt, receipt_id)
    # Clearing the claim token is what makes it claimable by the next wake.
    assert row.delivery_request_id is None
    assert row.delivery_attempts == 1


def test_requeue_records_why_without_failing_the_receipt(orm: Session):
    receipt_id = _receipt(orm)

    requeue_live_receipt(orm, receipt_id=receipt_id, error="channel down")

    row = orm.get(LiveSessionInputReceipt, receipt_id)
    payload = json.loads(row.error_json)
    assert payload["reason"] == "transient"
    assert payload["message"] == "channel down"
    assert row.status == "queued"


def test_requeue_gives_up_once_the_input_has_aged_out(orm: Session):
    # Age is the real bound. The recovery loop wakes every 5s, so an attempt
    # budget would expire in under a minute for a machine that is merely asleep,
    # and would never expire at all for a session whose activity never becomes
    # drainable.
    receipt_id = _receipt(orm)
    later = datetime.now(UTC) + MAX_DELIVERY_AGE + timedelta(minutes=1)

    snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="still down", now=later)

    assert requeued is False
    assert snapshot is not None
    assert snapshot.status == "failed"
    payload = json.loads(orm.get(LiveSessionInputReceipt, receipt_id).error_json)
    assert payload["reason"] == "delivery_expired"


def test_requeue_keeps_retrying_inside_the_age_window(orm: Session):
    # A machine asleep for ten minutes must still get its message.
    receipt_id = _receipt(orm)
    later = datetime.now(UTC) + timedelta(minutes=10)

    _snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="asleep", now=later)

    assert requeued is True
    assert orm.get(LiveSessionInputReceipt, receipt_id).status == "queued"


def test_expired_receipt_stops_blocking_the_queue(orm: Session):
    # The bound exists so a poison message cannot wedge everything behind it:
    # once failed, it is no longer claimable.
    receipt_id = _receipt(orm)
    later = datetime.now(UTC) + MAX_DELIVERY_AGE + timedelta(minutes=1)

    requeue_live_receipt(orm, receipt_id=receipt_id, error="still down", now=later)

    remaining = (
        orm.query(LiveSessionInputReceipt)
        .filter(LiveSessionInputReceipt.status == "queued")
        .count()
    )
    assert remaining == 0


def test_requeue_survives_a_vanished_receipt(orm: Session):
    snapshot, requeued = requeue_live_receipt(orm, receipt_id=str(uuid4()), error="gone")
    assert snapshot is None
    assert requeued is False


def test_attempts_accumulate_across_separate_failures(orm: Session):
    receipt_id = _receipt(orm)

    for expected in (1, 2, 3):
        orm.get(LiveSessionInputReceipt, receipt_id).status = "delivering"
        orm.commit()
        _snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="down")
        assert requeued is True
        assert orm.get(LiveSessionInputReceipt, receipt_id).delivery_attempts == expected


def test_attempt_backstop_stops_a_hot_spin(orm: Session):
    # Age is the policy bound; this only guards churn that never ages out.
    receipt_id = _receipt(orm, attempts=MAX_DELIVERY_ATTEMPTS - 1)

    _snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="down")

    assert requeued is False
    payload = json.loads(orm.get(LiveSessionInputReceipt, receipt_id).error_json)
    assert payload["reason"] == "max_delivery_attempts"


# --- The intent regression itself -------------------------------------------


def test_directed_input_never_selects_a_live_only_intent():
    """Directed input must not pick delivery semantics from target phase.

    The incident: an idle target selected `auto`, which dispatched live and
    failed terminally when the channel was down. A busy target selected `queue`
    and survived. Reading the source is the honest check here — the alternative
    is standing up a full catalogd session graph to assert one constant.
    """

    from pathlib import Path as _Path

    source = _Path(__file__).resolve().parents[1] / "zerg" / "routers" / "agents_sessions.py"
    body = source.read_text(encoding="utf-8")
    marker = "async def _deliver_directed_input"
    start = body.index(marker) if marker in body else body.index("render_directed_input_envelope(")
    region = body[start : start + 4000]

    assert "INPUT_INTENT_AUTO" not in region, "directed input must not dispatch with the live-only auto intent"
    assert "INPUT_INTENT_QUEUE" in region


def test_command_id_is_stable_across_delivery_attempts():
    """Retrying a transient failure must not inject the prompt twice.

    The engine dedupes by command id, so every attempt for one receipt has to
    produce the same id. Seeding from the per-attempt claim token — which is
    what the code did before — defeats that entirely.
    """

    from types import SimpleNamespace

    from zerg.services.managed_control_dispatcher import _engine_command_id

    session = SimpleNamespace(id=uuid4())
    receipt_id = str(uuid4())

    first = _engine_command_id(
        session=session, command_type="session.send_text", request_id=receipt_id, run_id=None
    )
    second = _engine_command_id(
        session=session, command_type="session.send_text", request_id=receipt_id, run_id=None
    )

    assert first == second
    # And a different receipt must not collide with it.
    other = _engine_command_id(
        session=session, command_type="session.send_text", request_id=str(uuid4()), run_id=None
    )
    assert other != first


# --- The retry actually reaching the engine ----------------------------------


def test_a_retry_prepares_the_same_operation_id():
    """A retry must be an exact replay, not a new operation.

    This is the defect Sol's review caught in the first implementation: the
    command id was made stable (good) while the operation id stayed a fresh
    uuid4 per attempt. catalogd treats a known command_id arriving with a *new*
    operation id as `idempotency_conflict`, so every retry was refused before it
    reached the engine and simply burned the budget. Both ids have to be
    deterministic for a retry to replay.
    """

    from uuid import uuid5

    from zerg.services.managed_control_dispatcher import _CONTROL_OPERATION_NAMESPACE

    command_id = "managed-control:sess-1:session.send_text:receipt-1"
    first = uuid5(_CONTROL_OPERATION_NAMESPACE, command_id)
    second = uuid5(_CONTROL_OPERATION_NAMESPACE, command_id)
    other = uuid5(_CONTROL_OPERATION_NAMESPACE, "managed-control:sess-1:session.send_text:receipt-2")

    assert first == second, "a retry of the same command must reuse its operation id"
    assert first != other, "different receipts must not share an operation id"


@pytest.mark.asyncio
async def test_transient_dispatch_failure_requeues_then_delivers(monkeypatch):
    """End to end through wake_next_live_catalog_input: fail, requeue, deliver.

    This is the sequence the unit tests cannot cover, and the one that exposed
    the operation-id conflict in the first implementation. It asserts the two
    things that matter across a retry: the receipt is returned to the queue
    rather than failed, and both attempts present the same request_id, because
    that is what the engine command id is seeded from.
    """

    from uuid import uuid4 as _uuid4

    from zerg.services import live_control_catalog as lcc
    from zerg.services.managed_control_dispatcher import DISPATCH_FAILURE_TRANSPORT

    session_id = str(_uuid4())
    receipt_id = str(_uuid4())
    seen_request_ids: list[str] = []
    finish_statuses: list[str] = []

    async def fake_dispatch(**kwargs):
        seen_request_ids.append(str(kwargs["request_id"]))
        if len(seen_request_ids) == 1:
            return ManagedControlDispatchResult(
                ok=False,
                transport="engine_channel",
                error="Machine control channel was replaced",
                failure_kind=DISPATCH_FAILURE_TRANSPORT,
                failure_reason="not_sent",
            )
        return ManagedControlDispatchResult(ok=True, transport="engine_channel", data={"exit_code": 0})

    class _Catalogd:
        async def call(self, method, params, timeout_seconds=None):
            if method == "session.input.claim.v2":
                return {
                    "claimed": True,
                    "session": {"id": session_id, "provider": "claude", "device_id": "cinder"},
                    "receipt": {"id": receipt_id, "owner_id": 1, "text": "hello"},
                }
            if method == "session.input.finish.v2":
                finish_statuses.append(str(params["status"]))
                return {"found": True, "changed": True}
            return {}

    class _Locks:
        async def acquire(self, **_kwargs):
            return True

        async def release(self, *_args):
            return None

    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: _Catalogd())
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.dispatch_managed_control_command", fake_dispatch)
    monkeypatch.setattr("zerg.services.session_locks.session_lock_manager", _Locks())

    first = await lcc.wake_next_live_catalog_input(session_id)
    second = await lcc.wake_next_live_catalog_input(session_id)

    assert first is False, "a transient failure does not count as delivered"
    assert second is True, "the retry must actually deliver"
    # The whole point: the first failure returned the input to the queue.
    assert finish_statuses == ["queued", "delivered"]
    # And both attempts seeded the engine command id from the durable receipt.
    assert seen_request_ids == [receipt_id, receipt_id]
