"""Disposable derived search/worklog index owned by the searchd process."""

# Pin SQLite before the store binds sqlite3. `python -m zerg.searchd` imports
# this package, and through it the store, before __main__ runs, so the
# entrypoint's bootstrap import alone left searchd on the image's stdlib
# SQLite 3.40.1. (Not in zerg/__init__: the image's provisioning stage copies
# only part of the package and must import zerg without this module.)
import zerg.bootstrap_sqlite  # noqa: F401  # isort: skip

from zerg.searchd.server import SearchDaemon

__all__ = ["SearchDaemon"]
