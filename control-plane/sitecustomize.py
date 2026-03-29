"""Prefer the vendored pysqlite3 build when available."""

from __future__ import annotations

import sys

try:
    import pysqlite3
except ImportError:
    pass
else:
    sys.modules["sqlite3"] = pysqlite3
    dbapi2 = getattr(pysqlite3, "dbapi2", None)
    if dbapi2 is not None:
        sys.modules["sqlite3.dbapi2"] = dbapi2
