"""Timezone helpers – provide a single UTC-aware *now()* function.

The codebase historically mixed naive ``datetime.now()`` calls and UTC-aware
``datetime.now(tz=timezone.utc)``.  To ensure consistency going forward we
import :pyfunc:`utc_now` everywhere instead of calling the stdlib helpers
directly.

``UTCBaseModel`` is a Pydantic BaseModel that serializes naive datetimes
with a trailing "Z" so that JavaScript ``new Date()`` correctly interprets
them as UTC rather than local time.
"""

from datetime import datetime
from datetime import timezone

from pydantic import BaseModel
from pydantic import ConfigDict


def utc_now() -> datetime:  # noqa: D401 – simple utility
    """Return *aware* current time in UTC."""

    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:  # noqa: D401 – simple utility
    """Return *naive* current time in UTC for database compatibility.

    SQLAlchemy DateTime columns without timezone info store naive datetimes.
    This function provides UTC time in the format expected by the database.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UTCBaseModel(BaseModel):
    """Pydantic BaseModel that appends 'Z' to naive datetime fields on serialization.

    SQLite stores naive datetimes that are actually UTC. Without timezone info,
    ``datetime.isoformat()`` omits the 'Z' suffix and JavaScript ``new Date()``
    treats them as local time. This base model fixes the root cause for all
    API response models.
    """

    @staticmethod
    def _serialize_datetime(dt: datetime) -> str:
        if dt.tzinfo is None:
            return f"{dt.isoformat()}Z"

        normalized = dt.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")

    model_config = ConfigDict(
        json_encoders={
            datetime: _serialize_datetime,
        }
    )


__all__ = ["utc_now", "utc_now_naive", "UTCBaseModel"]
