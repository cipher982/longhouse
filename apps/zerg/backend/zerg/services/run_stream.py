from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from zerg.database import db_session
from zerg.database import reset_test_commis_id
from zerg.database import set_test_commis_id
from zerg.services.event_store import EventStore


@dataclass(frozen=True)
class HistoricalRunEvent:
    event_id: int
    event_type: str
    payload: dict
    timestamp: str


@contextmanager
def with_test_commis_routing(test_commis_id: str | None) -> Iterator[None]:
    token = set_test_commis_id(test_commis_id) if test_commis_id else None
    try:
        yield
    finally:
        if token is not None:
            reset_test_commis_id(token)


def load_historical_run_events(
    run_id: int,
    after_event_id: int,
    include_tokens: bool,
    *,
    test_commis_id: str | None = None,
) -> list[HistoricalRunEvent]:
    events: list[HistoricalRunEvent] = []

    with with_test_commis_routing(test_commis_id):
        with db_session() as db:
            historical = EventStore.get_events_after(
                db=db,
                run_id=run_id,
                after_id=after_event_id,
                include_tokens=include_tokens,
            )
            for event in historical:
                events.append(
                    HistoricalRunEvent(
                        event_id=event.id,
                        event_type=event.event_type,
                        payload=event.payload,
                        timestamp=event.created_at.isoformat().replace("+00:00", "Z"),
                    )
                )

    return events
