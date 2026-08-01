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
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.services.live_control_catalog import _is_transient_delivery_failure
from zerg.services.live_session_inputs import MAX_DELIVERY_ATTEMPTS
from zerg.services.live_session_inputs import requeue_live_receipt
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


def test_absent_transport_is_transient():
    # Nothing received the input, so nothing rejected it. The machine may be
    # asleep; consuming the message here is what caused the incident.
    result = ManagedControlDispatchResult(
        ok=False,
        transport=MANAGED_CONTROL_TRANSPORT_NONE,
        error=MANAGED_CONTROL_UNAVAILABLE_ERROR,
    )
    assert _is_transient_delivery_failure(result) is True


@pytest.mark.parametrize(
    "error",
    [
        "command timed out after 15s",
        "engine channel not connected",
        "peer disconnected mid-command",
    ],
)
def test_ambiguous_transport_errors_are_transient(error: str):
    # Acceptance is unknown. Redelivery is safe only because the command id is
    # seeded from the durable receipt, so the engine dedupes a repeat.
    result = ManagedControlDispatchResult(ok=False, transport="engine_channel", error=error)
    assert _is_transient_delivery_failure(result) is True


def test_provider_rejection_is_terminal():
    # The provider saw the input and refused it. Retrying forever would block
    # every later input behind a message that will never land.
    result = ManagedControlDispatchResult(
        ok=False,
        transport="engine_channel",
        error="provider rejected the request: malformed payload",
    )
    assert _is_transient_delivery_failure(result) is False


def test_missing_error_on_a_live_transport_is_terminal():
    # A non-zero exit with no transport error is a real refusal, not a gap.
    result = ManagedControlDispatchResult(ok=False, transport="engine_channel", error=None)
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


def test_requeue_gives_up_after_the_attempt_bound(orm: Session):
    # An unreachable machine must not hold its queue open forever, and an input
    # sent hours ago is no longer something the user wants injected.
    receipt_id = _receipt(orm, attempts=MAX_DELIVERY_ATTEMPTS - 1)

    snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="still down")

    assert requeued is False
    assert snapshot is not None
    assert snapshot.status == "failed"
    payload = json.loads(orm.get(LiveSessionInputReceipt, receipt_id).error_json)
    assert payload["reason"] == "max_delivery_attempts"
    assert payload["attempts"] == MAX_DELIVERY_ATTEMPTS


def test_exhausted_receipt_stops_blocking_the_queue(orm: Session):
    # The bound exists so a poison message cannot wedge everything behind it:
    # once failed, it is no longer claimable.
    receipt_id = _receipt(orm, attempts=MAX_DELIVERY_ATTEMPTS - 1)

    requeue_live_receipt(orm, receipt_id=receipt_id, error="still down")

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

    for expected in range(1, MAX_DELIVERY_ATTEMPTS):
        orm.get(LiveSessionInputReceipt, receipt_id).status = "delivering"
        orm.commit()
        _snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="down")
        assert requeued is True
        assert orm.get(LiveSessionInputReceipt, receipt_id).delivery_attempts == expected

    orm.get(LiveSessionInputReceipt, receipt_id).status = "delivering"
    orm.commit()
    _snapshot, requeued = requeue_live_receipt(orm, receipt_id=receipt_id, error="down")
    assert requeued is False


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
