"""Open a provider's SQLite store for reading, without writing to it.

Cursor's ``store.db`` and OpenCode's ``opencode.db`` are the providers' own
files. Every QA producer and oracle that reads one opens it here, so the
verifier has one answer to how such a store is opened.

The verifier does not import the code it judges, so the product's one reader
of a provider store, ``zerg.services.cursor_transcript``, keeps its own
opener; ``tests_lite/test_provider_sqlite.py`` holds the two to the same
behaviour. This module imports only the standard library:
``scripts/qa/transcript_census.py`` loads it by path, outside the ``zerg``
package.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# The two ways SQLite says it could not read a store through its write-ahead
# log: SQLITE_READONLY and SQLITE_CANTOPEN, by number so that this also loads
# on a Python whose sqlite3 does not export the names.
_UNREADABLE_THROUGH_WAL = (8, 14)
_UNREADABLE_MESSAGES = ("attempt to write a readonly database", "unable to open database file")


def _needs_the_main_file_alone(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code is None:
        return str(error).startswith(_UNREADABLE_MESSAGES)
    # Extended codes carry the primary code in their low byte.
    return code & 0xFF in _UNREADABLE_THROUGH_WAL


def open_readonly(path: Path | str, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a provider store read-only and WAL-aware.

    ``mode=ro`` reads the write-ahead log, which is where a store still being
    written keeps its newest rows; WAL readers do not block the writer.
    It also needs the log's index (``-shm``). A store whose writer exited
    cleanly has none, and when its directory cannot take a new one the open
    still succeeds: it is the first read that fails. So a read decides, and
    only then is the store opened ``immutable=1``, which reads the main file
    alone, all a closed store has. A store that is not there is an error
    either way, and is never created.

    Any other failure is raised as it is. A store that is locked or busy is
    still being written, and its main file alone would be a stale answer.
    """
    target = Path(path).absolute().as_uri()
    try:
        connection = sqlite3.connect(f"{target}?mode=ro", uri=True, timeout=timeout)
        try:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.OperationalError:
            connection.close()
            raise
        return connection
    except sqlite3.OperationalError as error:
        if not _needs_the_main_file_alone(error):
            raise
        return sqlite3.connect(f"{target}?mode=ro&immutable=1", uri=True, timeout=timeout)
