from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionObservation
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.session_observation_reducers import reduce_provider_event_observation
from zerg.services.session_observation_reducers import reduce_source_line_observation
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_observations import SOURCE_DOMAIN_RUNTIME
from zerg.services.session_observations import decode_observation_payload_json
from zerg.services.session_observations import record_provider_event_observation
from zerg.services.session_observations import record_session_observation
from zerg.services.session_observations import record_source_line_observation


def test_provider_event_observation_payload_is_compressed_and_reduced(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw_json = '{"type":"assistant","uuid":"evt-1","message":{"content":[{"type":"text","text":"hello"}]}}'

    with factory() as db:
        _seed_session(db, session_id)
        result = record_provider_event_observation(
            db,
            session_id=session_id,
            provider="claude",
            device_id="device-1",
            source="agents_ingest",
            branch_id=1,
            role="assistant",
            timestamp=_ts(),
            event_hash="hash-1",
            content_text="hello",
            source_path="/tmp/session.jsonl",
            source_offset=10,
            raw_json=raw_json,
        )
        assert result.observation is not None

        observation = result.observation
        assert observation.payload_json == ""
        assert observation.payload_json_z is not None
        assert observation.payload_json_codec == CODEC_ZSTD
        decoded = json.loads(decode_observation_payload_json(observation) or "{}")
        assert decoded["raw_json"] == raw_json

        reduction = reduce_provider_event_observation(db, observation)
        assert reduction.inserted is True
        event = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).one()
        assert event.content_text == "hello"
        assert event.raw_json_z is not None


def test_source_line_observation_payload_is_compressed_and_reduced(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw_json = '{"type":"user","message":{"content":"hi"}}'

    with factory() as db:
        _seed_session(db, session_id)
        result = record_source_line_observation(
            db,
            session_id=session_id,
            provider="claude",
            device_id="device-1",
            source="agents_ingest",
            source_path="/tmp/session.jsonl",
            source_offset=10,
            branch_id=1,
            revision=1,
            line_hash="line-hash-1",
            raw_json=raw_json,
            observed_at=_ts(),
        )
        assert result.observation is not None

        observation = result.observation
        assert observation.payload_json == ""
        assert observation.payload_json_z is not None
        assert observation.payload_json_codec == CODEC_ZSTD
        decoded = json.loads(decode_observation_payload_json(observation) or "{}")
        assert decoded["raw_json"] == raw_json

        row = reduce_source_line_observation(db, observation)
        assert row is not None
        source_line = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).one()
        assert source_line.line_hash == "line-hash-1"
        assert source_line.raw_json_z is not None


def test_runtime_observation_payload_stays_plain_for_sql_filters(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        result = record_session_observation(
            db,
            observation_id=f"runtime:{session_id}",
            session_id=session_id,
            runtime_key="runtime-1",
            provider="claude",
            device_id="device-1",
            source_domain=SOURCE_DOMAIN_RUNTIME,
            source="runtime",
            kind=OBS_KIND_RUNTIME_SIGNAL,
            observed_at=_ts(),
            payload={"kind": "phase_signal", "phase": "running"},
        )
        assert result.observation is not None
        observation = result.observation

        assert observation.payload_json_z is None
        assert observation.payload_json_codec == CODEC_PLAIN
        assert observation.payload_json is not None
        assert '"kind":"phase_signal"' in observation.payload_json
        assert decode_observation_payload_json(observation) == observation.payload_json


def test_decode_observation_payload_json_reads_legacy_plain_rows(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        observation = SessionObservation(
            observation_id=f"legacy:{session_id}",
            session_id=session_id,
            provider="claude",
            source_domain="transcript",
            source="legacy",
            kind="provider_event",
            observed_at=_ts(),
            payload_json='{"legacy":true}',
            payload_json_codec=CODEC_PLAIN,
        )
        db.add(observation)
        db.flush()

        assert decode_observation_payload_json(observation) == '{"legacy":true}'


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'observations.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, session_id: UUID) -> AgentSession:
    session = AgentSession(
        id=session_id,
        provider="claude",
        environment="test",
        project="longhouse",
        device_id="device-1",
        cwd="/tmp",
        started_at=_ts(),
        last_activity_at=_ts(),
    )
    db.add(session)
    db.flush()
    return session


def _ts() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)
