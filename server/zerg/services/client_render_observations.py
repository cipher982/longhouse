"""Shared reader for persisted client render observations.

`surface` and `managed` live in payload JSON, so those filters are applied
after the SQL query materializes candidate render observations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import SessionObservation
from zerg.services.session_observations import OBS_KIND_CLIENT_RENDER
from zerg.services.session_observations import SOURCE_DOMAIN_CLIENT
from zerg.utils.time import normalize_utc


@dataclass(frozen=True)
class ClientRenderObservation:
    row: SessionObservation
    payload: dict[str, Any]
    observation_id: str
    session_id: str | None
    provider: str | None
    surface: str | None
    managed: bool | None
    observed_at: datetime | None
    latency_ms: int | None
    ios_render_duration_ms: int | None


@dataclass(frozen=True)
class ClientRenderObservationList:
    rows: list[ClientRenderObservation]
    truncated: bool


def list_client_render_observations(
    db: Session,
    *,
    session_id: UUID | None = None,
    since: datetime | None = None,
    provider: str | None = None,
    surface: str | None = None,
    managed: bool | None = None,
    limit: int = 5_000,
) -> ClientRenderObservationList:
    provider = normalize_text_filter(provider)
    surface = normalize_text_filter(surface)
    limit = max(1, int(limit))
    query = (
        db.query(SessionObservation)
        .filter(SessionObservation.source_domain == SOURCE_DOMAIN_CLIENT)
        .filter(SessionObservation.kind == OBS_KIND_CLIENT_RENDER)
    )
    if session_id is not None:
        query = query.filter(SessionObservation.session_id == session_id)
    if since is not None:
        query = query.filter(SessionObservation.observed_at >= since)
    if provider is not None:
        query = query.filter(SessionObservation.provider == provider)

    raw_rows = (
        query.order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
        .limit(limit + 1)
        .all()
    )
    truncated = len(raw_rows) > limit
    observations: list[ClientRenderObservation] = []
    for row in raw_rows[:limit]:
        observation = build_client_render_observation(row)
        if surface is not None and observation.surface != surface:
            continue
        if managed is not None and observation.managed is not managed:
            continue
        observations.append(observation)
    return ClientRenderObservationList(rows=observations, truncated=truncated)


def build_client_render_observation(row: SessionObservation) -> ClientRenderObservation:
    payload = decode_payload(row.payload_json)
    webkit = payload.get("webkit") if isinstance(payload.get("webkit"), dict) else {}
    return ClientRenderObservation(
        row=row,
        payload=payload,
        observation_id=row.observation_id,
        session_id=str(row.session_id) if row.session_id else None,
        provider=row.provider or None,
        surface=text_or_none(payload.get("surface"), lowercase=True),
        managed=bool_or_none(payload.get("managed")),
        observed_at=normalize_utc(row.observed_at),
        latency_ms=int_or_none(payload.get("latency_ms")),
        ios_render_duration_ms=int_or_none(webkit.get("render_duration_ms")),
    )


def decode_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_text_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def text_or_none(value: Any, *, lowercase: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lower() if lowercase else text


def bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None
