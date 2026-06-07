"""Filesystem archive store wiring.

The archive is the durable raw-transcript store. The earlier hot/derived
physical-DB-split scaffolding (separate `hot.db`/`derived.db` engines, a
`derived_events` projection store, and the `LONGHOUSE_DATA_ROOT` /
`LONGHOUSE_HOT_DATABASE_URL` / `LONGHOUSE_DERIVED_DATABASE_URL` switches) was
never wired into any runtime path and has been removed; the product runs on a
single SQLite database plus this archive.
"""

from __future__ import annotations

from pathlib import Path

from zerg.config import Settings
from zerg.config import get_settings
from zerg.services.archive_store import FilesystemArchiveStore


def create_archive_store(settings: Settings | None = None) -> FilesystemArchiveStore:
    settings = settings or get_settings()
    return FilesystemArchiveStore(Path(settings.archive_root))
