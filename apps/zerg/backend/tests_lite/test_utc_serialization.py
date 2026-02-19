"""Tests for UTC datetime serialization.

Verifies that API response models serialize naive datetimes (from SQLite)
with a trailing "Z" suffix so that JS new Date() interprets them as UTC.
"""

from datetime import datetime
from datetime import timezone

from fastapi.encoders import jsonable_encoder

from zerg.utils.time import UTCBaseModel


# ---------------------------------------------------------------------------
# UTCBaseModel serialization
# ---------------------------------------------------------------------------


def test_naive_datetime_gets_z_suffix():
    """Naive datetimes (from SQLite) must serialize with 'Z' suffix."""

    class Sample(UTCBaseModel):
        ts: datetime

    m = Sample(ts=datetime(2024, 6, 15, 12, 30, 45))
    encoded = jsonable_encoder(m)
    assert encoded["ts"].endswith("Z"), f"Expected 'Z' suffix, got: {encoded['ts']}"
    assert encoded["ts"] == "2024-06-15T12:30:45Z"


def test_aware_datetime_keeps_offset():
    """Aware datetimes should keep their offset, not get double-suffixed."""

    class Sample(UTCBaseModel):
        ts: datetime

    m = Sample(ts=datetime(2024, 6, 15, 12, 30, 45, tzinfo=timezone.utc))
    encoded = jsonable_encoder(m)
    # Should be +00:00 (Pydantic default for aware UTC), NOT end with "ZZ"
    assert "ZZ" not in encoded["ts"]
    assert "+00:00" in encoded["ts"] or encoded["ts"].endswith("Z")


def test_optional_none_datetime():
    """Optional datetime fields that are None should serialize as None."""
    from typing import Optional

    class Sample(UTCBaseModel):
        ts: Optional[datetime] = None

    m = Sample(ts=None)
    encoded = jsonable_encoder(m)
    assert encoded["ts"] is None


def test_session_response_model():
    """SessionResponse (a real API model) should serialize datetimes with Z."""
    from zerg.routers.agents import SessionResponse

    resp = SessionResponse(
        id="test-session-id",
        project="test-project",
        provider="claude",
        started_at=datetime(2024, 6, 15, 10, 0, 0),
        ended_at=datetime(2024, 6, 15, 10, 30, 0),
        cwd="/home/user",
        user_messages=5,
        assistant_messages=10,
        tool_calls=3,
    )
    encoded = jsonable_encoder(resp)
    assert encoded["started_at"].endswith("Z"), f"started_at missing Z: {encoded['started_at']}"
    assert encoded["ended_at"].endswith("Z"), f"ended_at missing Z: {encoded['ended_at']}"


def test_model_dump_json_has_z():
    """model_dump_json() should also include Z suffix."""

    class Sample(UTCBaseModel):
        ts: datetime

    m = Sample(ts=datetime(2024, 1, 1, 0, 0, 0))
    json_str = m.model_dump_json()
    assert '"2024-01-01T00:00:00Z"' in json_str
