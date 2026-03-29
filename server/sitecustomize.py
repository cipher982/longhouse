"""Prefer the vendored pysqlite3 build when available.

The base Python image ships an older stdlib sqlite3 module. In production
containers we build pysqlite3 against a pinned upstream SQLite release and
alias it here so application imports transparently use the newer engine.
"""

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
