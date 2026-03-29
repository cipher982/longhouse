"""Bootstrap the pinned SQLite runtime for control-plane entrypoints."""

from __future__ import annotations

import sys


def bootstrap() -> None:
    """Alias sqlite3 to the pinned pysqlite3 build when present."""
    try:
        import pysqlite3
    except ImportError:
        return

    sys.modules["sqlite3"] = pysqlite3
    dbapi2 = getattr(pysqlite3, "dbapi2", None)
    if dbapi2 is not None:
        sys.modules["sqlite3.dbapi2"] = dbapi2


bootstrap()
