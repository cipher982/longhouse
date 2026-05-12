"""Reducers from raw session observations into read models."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import SessionObservation
from zerg.services.provisional_events import materialize_bridge_transcript_event
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA


def reduce_bridge_transcript_observation(db: Session, observation: SessionObservation) -> AgentEvent | None:
    if observation.kind != OBS_KIND_BRIDGE_TRANSCRIPT_DELTA:
        return None
    if observation.session_id is None:
        return None

    payload = _observation_payload(observation)
    bridge_payload = payload.get("payload")
    if not isinstance(bridge_payload, dict):
        return None

    return materialize_bridge_transcript_event(
        db,
        session_id=observation.session_id,
        provider=observation.provider,
        source=observation.source,
        occurred_at=observation.observed_at,
        received_at=observation.received_at,
        payload=bridge_payload,
    )


def _observation_payload(observation: SessionObservation) -> dict:
    raw = observation.payload_json
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
