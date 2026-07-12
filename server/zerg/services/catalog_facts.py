"""Hydration helpers for bounded raw facts returned by catalogd."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime

from zerg.services.catalog_read_gateway import session_batch_snapshot
from zerg.utils.time import normalize_utc


def decode_catalog_datetime(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return normalize_utc(parsed)


def hydrate_catalog_row(model, payload: dict[str, Any] | None):
    """Build one detached SQLAlchemy read model from a raw-facts payload."""

    if payload is None:
        return None
    values: dict[str, Any] = {}
    for column in model.__table__.columns:
        if column.name not in payload:
            continue
        value = payload[column.name]
        if value is not None and isinstance(column.type, DateTime):
            value = decode_catalog_datetime(value)
        values[column.name] = value
    return model(**values)


def session_facts_map(session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read up to any caller-sized set through bounded 100-session RPC pages."""

    result: dict[str, dict[str, Any]] = {}
    unique_ids = list(dict.fromkeys(session_ids))
    for offset in range(0, len(unique_ids), 20):
        snapshot = session_batch_snapshot(unique_ids[offset : offset + 20])
        for facts in snapshot.get("facts", []):
            if not isinstance(facts, dict):
                continue
            catalog = facts.get("catalog")
            session_id = str(catalog.get("session_id") or "") if isinstance(catalog, dict) else ""
            if session_id:
                result[session_id] = facts
    return result
