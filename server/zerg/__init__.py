# Pin SQLite before any subpackage binds the stdlib module. `python -m
# zerg.searchd` imports zerg.searchd/__init__ (and through it the store) before
# __main__ runs, so an entrypoint-level bootstrap alone left searchd on the
# image's stdlib SQLite 3.40.1, which rejects FTS5 contentless_delete.
import zerg.bootstrap_sqlite  # noqa: F401
