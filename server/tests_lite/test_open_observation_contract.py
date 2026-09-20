from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.live_store import LiveBase
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import _apply_runtime_event
from zerg.services.session_runtime import ingest_live_runtime_events


@pytest.fixture
def db_session():
    engine = make_engine("sqlite:///:memory:")
    LiveBase.metadata.create_all(engine)
    session_factory = make_sessionmaker(engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_event(*, kind: str, session_id: str | None = None) -> RuntimeEventIngest:
    sid = session_id or str(uuid4())
    return RuntimeEventIngest(
        runtime_key=f"omp:{sid}",
        session_id=sid,
        provider="omp",
        source="omp_helm_channel",
        kind=kind,
        occurred_at=datetime.now(timezone.utc),
        dedupe_key=f"omp-{kind}:{sid}:{uuid4()}",
    )


def test_open_observation_wire_contract_model_validation():
    # Unknown event kinds from newer machine engines are accepted on the wire
    ev_future = _make_event(kind="future_telemetry_probe")
    assert ev_future.kind == "future_telemetry_probe"

    ev_assertion = _make_event(kind="status_assertion")
    assert ev_assertion.kind == "status_assertion"

    # Enforces bounds
    with pytest.raises(ValidationError):
        _make_event(kind="")

    with pytest.raises(ValidationError):
        _make_event(kind="a" * 65)


def test_apply_runtime_event_ignores_unrecognized_kind(db_session):
    sid = str(uuid4())
    ev_unknown = _make_event(kind="unknown_experimental_metric", session_id=sid)

    outcome = _apply_runtime_event(db_session, ev_unknown)
    assert outcome == "ignored"

    # Unknown observations cannot create or mutate canonical hot runtime state.
    assert db_session.get(LiveRuntimeState, ev_unknown.runtime_key) is None
    assert db_session.get(LiveSession, sid) is None


def test_ingest_live_runtime_events_counts_ignored_without_mutating_liveness(db_session):
    sid1 = str(uuid4())
    sid2 = str(uuid4())

    ev_unknown = _make_event(kind="custom_sensor_data", session_id=sid1)
    ev_known = _make_event(kind="status_assertion", session_id=sid2)

    result = ingest_live_runtime_events(db_session, [ev_unknown, ev_known])

    assert result.accepted == 2
    assert result.ignored == 2  # status_assertion without existing state is ignored, custom_sensor_data is ignored
    assert result.duplicates == 0

    # LiveSession was not created for the unrecognized kind
    assert db_session.get(LiveSession, sid1) is None
