from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from typing import Iterator

from zerg.database import db_session
from zerg.database import reset_test_commis_id
from zerg.database import set_test_commis_id
from zerg.models.enums import RunStatus
from zerg.models.models import Run
from zerg.services.event_store import EventStore

TOOL_EVENTS_REQUIRING_RUN_ID = {
    "commis_tool_started",
    "commis_tool_completed",
    "commis_tool_failed",
    "commis_output_chunk",
}


@dataclass(frozen=True)
class HistoricalRunEvent:
    event_id: int
    event_type: str
    payload: dict
    timestamp: str


@dataclass
class StreamLifecycleState:
    pending_commiss: int = 0
    oikos_done: bool = False
    saw_oikos_complete: bool = False
    continuation_active: bool = False
    awaiting_continuation_until: float | None = None
    close_event_id: int | None = None
    stream_lease_until: float | None = None
    close_after_current_event: bool = False
    commis_grace_seconds: float = 5.0
    max_stream_ttl_ms: int = 300_000

    def apply(
        self,
        event_type: str,
        event: dict,
        *,
        from_replay: bool,
        now_monotonic: float,
    ) -> None:
        if event_type == "stream_control":
            action = event.get("action")
            ttl_ms = event.get("ttl_ms")
            current_event_id = event.get("event_id") or event.get("_event_id")

            if action == "close":
                self.close_event_id = current_event_id
                return

            if action == "keep_open":
                if ttl_ms and not from_replay:
                    capped_ttl = min(ttl_ms, self.max_stream_ttl_ms)
                    self.stream_lease_until = now_monotonic + (capped_ttl / 1000.0)
                self.awaiting_continuation_until = None
                return

        if event_type == "commis_spawned":
            self.pending_commiss += 1
            self.awaiting_continuation_until = None
            return

        if event_type in ("commis_complete", "commis_summary_ready"):
            if self.pending_commiss > 0:
                self.pending_commiss -= 1
            if self.pending_commiss == 0 and self.oikos_done and not self.continuation_active and not from_replay:
                if self.awaiting_continuation_until is None:
                    self.awaiting_continuation_until = now_monotonic + self.commis_grace_seconds
            return

        if event_type == "oikos_started":
            if self.saw_oikos_complete:
                self.continuation_active = True
            self.oikos_done = False
            self.awaiting_continuation_until = None
            return

        if event_type == "oikos_complete":
            self.saw_oikos_complete = True
            self.oikos_done = True
            if self.continuation_active:
                self.continuation_active = False
            if self.pending_commiss == 0 and not from_replay:
                self.close_after_current_event = True
            return

        if event_type == "oikos_deferred":
            if event.get("close_stream", True) and not from_replay:
                self.close_after_current_event = True
            return

        if event_type == "error" and not from_replay:
            self.close_after_current_event = True

    def should_close_after_replay(self, last_sent_event_id: int, status: RunStatus) -> bool:
        if self.close_event_id is not None and last_sent_event_id >= self.close_event_id:
            return True
        if self.saw_oikos_complete and self.pending_commiss == 0 and not self.continuation_active and self.close_event_id is None:
            return True
        return status not in (RunStatus.RUNNING, RunStatus.DEFERRED, RunStatus.WAITING)

    def next_timeout(self, now_monotonic: float) -> float:
        timeout_s = 30.0
        if self.awaiting_continuation_until is not None:
            remaining = self.awaiting_continuation_until - now_monotonic
            timeout_s = max(0.1, min(30.0, remaining))
        return timeout_s

    def should_close_on_timeout(self, now_monotonic: float) -> bool:
        if self.stream_lease_until is not None and now_monotonic >= self.stream_lease_until:
            return True
        return self.awaiting_continuation_until is not None and now_monotonic >= self.awaiting_continuation_until

    def should_close_after_live_event(self, event_id: int | None, now_monotonic: float) -> bool:
        if self.close_after_current_event:
            return True
        if self.awaiting_continuation_until is not None and now_monotonic >= self.awaiting_continuation_until:
            return True
        return self.close_event_id is not None and bool(event_id) and event_id >= self.close_event_id


class ContinuationAliasResolver:
    def __init__(self, *, root_run_id: int, test_commis_id: str | None = None) -> None:
        self.root_run_id = root_run_id
        self.test_commis_id = test_commis_id
        self._cache: dict[int, bool] = {}

    def is_continuation(self, candidate_run_id: int) -> bool:
        if candidate_run_id in self._cache:
            return self._cache[candidate_run_id]

        try:
            with with_test_commis_routing(self.test_commis_id):
                with db_session() as db:
                    candidate = db.query(Run).filter(Run.id == candidate_run_id).first()
                    if not candidate:
                        self._cache[candidate_run_id] = False
                        return False
                    is_continuation = bool(
                        candidate.root_run_id == self.root_run_id or candidate.continuation_of_run_id == self.root_run_id
                    )
                    self._cache[candidate_run_id] = is_continuation
                    return is_continuation
        except Exception:
            self._cache[candidate_run_id] = False
            return False


def filter_stream_event(
    event: dict[str, Any],
    *,
    owner_id: int,
    run_id: int,
    allow_continuation_runs: bool,
    continuation_resolver: ContinuationAliasResolver | None = None,
) -> dict[str, Any] | None:
    if event.get("owner_id") != owner_id:
        return None

    if "run_id" in event and event.get("run_id") != run_id:
        if not allow_continuation_runs or continuation_resolver is None:
            return None
        candidate_run_id = event.get("run_id")
        if not isinstance(candidate_run_id, int):
            return None
        if not continuation_resolver.is_continuation(candidate_run_id):
            return None
        event = dict(event)
        event["run_id"] = run_id

    event_type = event.get("event_type") or event.get("type")
    if event_type in TOOL_EVENTS_REQUIRING_RUN_ID and "run_id" not in event:
        return None

    return event


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
