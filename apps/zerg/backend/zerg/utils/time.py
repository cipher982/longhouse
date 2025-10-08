"""Timezone helpers – provide a single UTC-aware *now()* function.

The codebase historically mixed naive ``datetime.now()`` calls and UTC-aware
``datetime.now(tz=timezone.utc)``.  To ensure consistency going forward we
import :pyfunc:`utc_now` everywhere instead of calling the stdlib helpers
directly.
"""

from datetime import datetime
from datetime import timezone


def utc_now() -> datetime:  # noqa: D401 – simple utility
    """Return *aware* current time in UTC."""

    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:  # noqa: D401 – simple utility
    """Return *naive* current time in UTC for database compatibility.

    SQLAlchemy DateTime columns without timezone info store naive datetimes.
    This function provides UTC time in the format expected by the database.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


__all__ = ["utc_now", "utc_now_naive"]
