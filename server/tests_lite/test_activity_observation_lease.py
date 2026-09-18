"""Activity freshness is a lease from delivery, not a budget spent in transit.

A phase says what the provider was doing. How long that claim stays current is
a property of the path that carried it, and anchoring expiry to observation
time meant transit delay ate the window: on 2026-09-17 a delivery backlog made
every OMP session's activity arrive already expired, so sessions that were
alive and working dropped out of Live now.
"""

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import read_session_fact_heads
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.models import FactHead
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.machine_evidence import canonical_evidence_hash
from zerg.services.session_state_facts_projector import ACTIVITY_OBSERVATION_LEASE
from zerg.services.session_state_facts_projector import MAX_OBSERVATION_DELAY
from zerg.services.session_state_facts_projector import project_shadow_session_state_facts

SESSION_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"


def _at(minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 17, 15, minute, second, tzinfo=timezone.utc)


def _catalog_facts() -> dict:
    return {
        "catalog": {"session_id": SESSION_ID},
        "latest_run": {"id": RUN_ID, "ended_at": None, "started_at": _at().isoformat()},
        "connections": [],
    }


def _activity_head(
    *,
    phase: str,
    observed_at: datetime,
    received_at: datetime,
    contract_window: timedelta,
) -> dict:
    value = {
        "authority_class": "provider_runtime",
        "provider": "omp",
        "session_id": SESSION_ID,
        "run_id": RUN_ID,
        "kind": phase,
        "raw_kind": phase,
        "tool_name": None,
        "source": "omp_helm_channel",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + contract_window).isoformat(),
    }
    return {
        "family": "activity",
        "subject_key": f"run:{RUN_ID}",
        "source": "omp_helm_channel",
        "source_epoch": RUN_ID,
        "session_id": SESSION_ID,
        "ordering_mode": "observed_at",
        "source_seq": None,
        "evidence_hash": canonical_evidence_hash(value),
        "observed_at": observed_at,
        "valid_until": observed_at + contract_window,
        "value_json": json.dumps(value),
        "raw_locator": None,
        "updated_commit_seq": 1,
        "received_at": received_at,
    }


def _project(head: dict, *, now: datetime):
    return project_shadow_session_state_facts(
        session_id=SESSION_ID,
        commit_seq=1,
        catalog_facts=_catalog_facts(),
        heads=[head],
        supported_operations=(),
        now=now,
    )


def test_transit_delay_no_longer_expires_a_working_session():
    """The incident, as one assertion.

    A `thinking` observation whose 90s window was nearly spent in transit is
    current when it lands, because the lease starts on delivery.
    """

    observed_at = _at()
    received_at = observed_at + timedelta(seconds=50)

    projection = _project(
        _activity_head(
            phase="thinking",
            observed_at=observed_at,
            received_at=received_at,
            contract_window=timedelta(seconds=90),
        ),
        now=received_at + timedelta(seconds=30),
    )

    assert projection.activity.state == "thinking"
    assert projection.activity.valid_until == received_at + ACTIVITY_OBSERVATION_LEASE


def test_a_session_that_stops_reporting_still_goes_unknown():
    """The lease is a lease: silence expires it."""

    observed_at = _at()
    received_at = observed_at + timedelta(seconds=1)

    projection = _project(
        _activity_head(
            phase="thinking",
            observed_at=observed_at,
            received_at=received_at,
            contract_window=timedelta(seconds=90),
        ),
        now=received_at + ACTIVITY_OBSERVATION_LEASE + timedelta(seconds=120),
    )

    assert projection.activity.state == "unknown"


def test_a_backlog_drained_later_does_not_look_current():
    """A delayed delivery is history. It gets no lease at all."""

    observed_at = _at()
    received_at = observed_at + MAX_OBSERVATION_DELAY + timedelta(seconds=30)

    projection = _project(
        _activity_head(
            phase="thinking",
            observed_at=observed_at,
            received_at=received_at,
            contract_window=timedelta(seconds=90),
        ),
        now=received_at + timedelta(seconds=1),
    )

    assert projection.activity.state == "unknown"
    assert projection.activity.valid_until == observed_at + timedelta(seconds=90)


def test_the_lease_never_shortens_a_contract_window():
    """`blocked` waits on a person, not on a keepalive.

    The lease is a floor. A session waiting for permission keeps its day-long
    window, because expiring it to `unknown` would lose the one fact an
    operator needs.
    """

    observed_at = _at()
    received_at = observed_at + timedelta(seconds=2)
    day = timedelta(hours=24)

    projection = _project(
        _activity_head(
            phase="blocked",
            observed_at=observed_at,
            received_at=received_at,
            contract_window=day,
        ),
        now=received_at + timedelta(minutes=30),
    )

    assert projection.activity.state == "blocked"
    assert projection.activity.valid_until == observed_at + day


def test_a_redelivered_observation_cannot_renew_the_lease():
    """Replay safety, structurally.

    The lease is anchored to the head's receipt. A re-delivered copy of an
    observation is a duplicate that never becomes the head, so its arrival
    cannot extend anything — the projection is identical no matter how many
    times the same event is posted.
    """

    observed_at = _at()
    received_at = observed_at + timedelta(seconds=5)
    head = _activity_head(
        phase="thinking",
        observed_at=observed_at,
        received_at=received_at,
        contract_window=timedelta(seconds=90),
    )

    first = _project(head, now=received_at + timedelta(seconds=10))
    replayed = _project(head, now=received_at + timedelta(seconds=40))

    assert first.activity.valid_until == replayed.activity.valid_until
    assert replayed.activity.valid_until == max(
        observed_at + timedelta(seconds=90),
        received_at + ACTIVITY_OBSERVATION_LEASE,
    )


def test_an_unreadable_receipt_costs_the_lease_not_the_head():
    """The lease may never reject a head.

    SQLite hands back naive datetimes, and a receipt this code cannot parse is
    not evidence that the session is dead. Raising here would serve a live
    session as `unknown`, which is the failure the lease exists to prevent.
    """

    observed_at = _at()
    for unreadable in (None, "", "not-a-timestamp", 1758114000):
        head = _activity_head(
            phase="thinking",
            observed_at=observed_at,
            received_at=observed_at,
            contract_window=timedelta(seconds=90),
        )
        head["received_at"] = unreadable

        projection = _project(head, now=observed_at + timedelta(seconds=30))

        assert projection.activity.state == "thinking", unreadable
        assert projection.activity.valid_until == observed_at + timedelta(seconds=90)

    naive = _activity_head(
        phase="thinking",
        observed_at=observed_at,
        received_at=observed_at,
        contract_window=timedelta(seconds=90),
    )
    naive["received_at"] = (observed_at + timedelta(seconds=50)).replace(tzinfo=None)

    projection = _project(naive, now=observed_at + timedelta(seconds=60))

    assert projection.activity.state == "thinking"
    assert projection.activity.valid_until == observed_at + timedelta(seconds=50) + ACTIVITY_OBSERVATION_LEASE


def test_the_lease_outlasts_the_slowest_producer_keepalive():
    """A lease shorter than a producer's keepalive flaps a healthy session."""

    assert ACTIVITY_OBSERVATION_LEASE >= timedelta(seconds=40)


@pytest.fixture
def catalog_engine(tmp_path: Path):
    engine = create_catalog_engine(tmp_path / f"lease-{uuid4().hex[:8]}.db")
    initialize_catalog_schema(engine)
    yield engine
    engine.dispose()


def _reduce_activity(engine, *, observed_at: datetime, received_at: datetime, seq: int = 1):
    """Put one activity observation through the real reducer."""

    value = {
        "authority_class": "provider_runtime",
        "provider": "omp",
        "session_id": SESSION_ID,
        "run_id": RUN_ID,
        "kind": "thinking",
        "raw_kind": "thinking",
        "tool_name": None,
        "source": "omp_helm_channel",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(seconds=90)).isoformat(),
    }
    with engine.begin() as connection:
        result = reduce_fact_batch(
            connection,
            [
                ReducerFact(
                    family="activity",
                    subject_key=f"run:{RUN_ID}",
                    source="omp_helm_channel",
                    source_epoch=RUN_ID,
                    source_seq=seq,
                    dedupe_key=canonical_evidence_hash(value),
                    evidence_hash=canonical_evidence_hash(value),
                    value=value,
                    observed_at=observed_at,
                    session_id=SESSION_ID,
                    valid_until=observed_at + timedelta(seconds=90),
                    raw_locator="runtime:omp_helm_channel:test",
                )
            ],
            received_at=received_at,
        )
    return result


def _head_received_at(engine) -> datetime:
    with engine.begin() as connection:
        return connection.execute(
            select(FactHead.__table__.c.received_at).where(FactHead.__table__.c.family == "activity")
        ).scalar_one()


def _project_stored_heads(engine, *, now: datetime):
    with engine.begin() as connection:
        _commit_seq, heads = read_session_fact_heads(connection, session_id=SESSION_ID)
    return project_shadow_session_state_facts(
        session_id=SESSION_ID,
        commit_seq=1,
        catalog_facts=_catalog_facts(),
        heads=heads,
        supported_operations=(),
        now=now,
    )


def test_a_replayed_observation_does_not_renew_the_stored_lease(catalog_engine):
    """Replay safety through the reducer, not around it.

    The earlier version of this test reused one in-memory head, which proved
    nothing: it could not tell a duplicate from a new observation because it
    never asked the thing that decides. This one replays the identical fact
    through the real reducer and asserts the head's receipt does not move.
    """

    observed_at = _at()
    first_receipt = observed_at + timedelta(seconds=5)

    first = _reduce_activity(catalog_engine, observed_at=observed_at, received_at=first_receipt)
    assert first.changed_heads == 1, "the first observation becomes the head"
    assert _head_received_at(catalog_engine).replace(tzinfo=timezone.utc) == first_receipt

    # The same observation arrives again, a minute later.
    replay = _reduce_activity(
        catalog_engine,
        observed_at=observed_at,
        received_at=first_receipt + timedelta(seconds=60),
    )

    assert replay.changed_heads == 0, "a duplicate never becomes the head"
    assert replay.duplicates == 1
    assert _head_received_at(catalog_engine).replace(tzinfo=timezone.utc) == first_receipt, (
        "the lease anchor did not move, so the replay renewed nothing"
    )

    projection = _project_stored_heads(
        catalog_engine,
        now=first_receipt + ACTIVITY_OBSERVATION_LEASE + timedelta(seconds=120),
    )
    assert projection.activity.state == "unknown", (
        "a session that only ever had one observation still expires, however often it is resent"
    )


def test_an_old_observation_gets_no_lease_even_as_a_fresh_head(catalog_engine):
    """The delay cap is what bounds every 'could a head be rewritten' path.

    Whatever makes an old observation into a new head — an epoch that rotated,
    a pruned head, a restored backup — the cap decides: delivery long after
    the fact earns no lease at all, so it cannot masquerade as current.
    """

    observed_at = _at()
    late_receipt = observed_at + MAX_OBSERVATION_DELAY + timedelta(minutes=30)

    result = _reduce_activity(catalog_engine, observed_at=observed_at, received_at=late_receipt)
    assert result.changed_heads == 1, "it did become the head"

    projection = _project_stored_heads(catalog_engine, now=late_receipt + timedelta(seconds=1))

    assert projection.activity.state == "unknown"
    assert projection.activity.valid_until == observed_at + timedelta(seconds=90)
