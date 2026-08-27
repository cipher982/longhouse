"""E2E tests for datetime serialization.

Verifies that actual API endpoints return datetime fields with "Z" suffix
so that JavaScript clients parse them correctly as UTC.

The sessions the browser reads come out of the live catalog, so the payloads
checked here are assembled by the projection a Runtime Host runs -- not by a
SQLAlchemy row read that production no longer performs.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401

OWNER_EMAIL = "owner@datetime.test"
DEVICE_ID = "cinder"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(live_catalog):
    """One recent session in the live catalog, plus the token that reads it."""

    owner_id = live_catalog.create_user(OWNER_EMAIL)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)
    seeded = live_catalog.commit_session(
        owner_id=owner_id,
        texts=("Test message",),
        device_id=DEVICE_ID,
        now=datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1),
    )
    return seeded, {"X-Agents-Token": token}


def _find_datetime_strings(obj, path=""):
    """Recursively find all string values that look like ISO datetimes."""
    datetime_fields = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f"{path}.{key}" if path else key
            datetime_fields.extend(_find_datetime_strings(value, current_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            current_path = f"{path}[{i}]"
            datetime_fields.extend(_find_datetime_strings(item, current_path))
    elif isinstance(obj, str):
        # Check if it looks like an ISO datetime
        if "T" in obj and (":" in obj or "-" in obj):
            # Heuristic: has T separator and time/date components
            datetime_fields.append((path, obj))

    return datetime_fields


def _assert_all_utc_suffixed(data) -> None:
    datetime_fields = _find_datetime_strings(data)

    # Sanity check: a payload with no datetimes proves nothing.
    assert datetime_fields, f"Expected to find datetime fields in response. Got: {data}"

    failures = [f"{path} = {value} (missing Z suffix)" for path, value in datetime_fields if not value.endswith("Z")]
    assert not failures, "Found datetime fields without Z suffix:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sessions_endpoint_datetime_has_z_suffix(live_catalog, live_catalog_client):
    """GET /api/agents/sessions should return all datetimes with Z suffix."""

    _seeded, headers = _seed(live_catalog)

    response = live_catalog_client.get("/agents/sessions", headers=headers)
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload["total"] == 1, payload
    _assert_all_utc_suffixed(payload)


def test_session_detail_endpoint_datetime_has_z_suffix(live_catalog, live_catalog_client):
    """GET /api/agents/sessions/:id should return all datetimes with Z suffix."""

    seeded, headers = _seed(live_catalog)

    response = live_catalog_client.get(f"/agents/sessions/{seeded.session_id}", headers=headers)
    assert response.status_code == 200, response.text

    _assert_all_utc_suffixed(response.json())
