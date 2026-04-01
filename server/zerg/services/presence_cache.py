"""In-memory presence cache to eliminate per-hook SQLite writes.

Presence state is ephemeral (10-min TTL). Writing to SQLite on every
Claude Code hook event causes write-lock contention under load.

This module keeps presence in a process-local dict and flushes dirty
entries to SQLite on a timer (default 5s). The read path
(_load_presence_map in agents.py) reads from memory, falling back to
DB on cold start.

Runtime events and operator wakeups still write to DB immediately —
they're low-frequency side effects, not per-hook.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone

from zerg.models.agents import SessionPresence

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 5


@dataclass
class PresenceEntry:
    """In-memory mirror of a SessionPresence row."""

    session_id: str
    state: str
    tool_name: str | None = None
    device_id: str | None = None
    cwd: str | None = None
    project: str | None = None
    provider: str = "claude"
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    dirty: bool = True  # needs DB flush


class PresenceCache:
    """Process-singleton in-memory presence store."""

    def __init__(self) -> None:
        self._entries: dict[str, PresenceEntry] = {}
        self._flush_task: asyncio.Task | None = None
        self._cold: bool = True  # True until first DB load

    # -- write path (called from presence router) --

    def upsert(
        self,
        session_id: str,
        state: str,
        *,
        tool_name: str | None = None,
        device_id: str | None = None,
        cwd: str | None = None,
        project: str | None = None,
        provider: str = "claude",
        updated_at: datetime | None = None,
    ) -> tuple[PresenceEntry, PresenceEntry | None]:
        """Update presence in memory. Returns (new_entry, previous_entry_or_None)."""
        now = updated_at or datetime.now(timezone.utc)
        previous = self._entries.get(session_id)
        prev_snapshot = None
        if previous is not None:
            # Snapshot for caller to compare states
            prev_snapshot = PresenceEntry(
                session_id=previous.session_id,
                state=previous.state,
                tool_name=previous.tool_name,
                device_id=previous.device_id,
                cwd=previous.cwd,
                project=previous.project,
                provider=previous.provider,
                updated_at=previous.updated_at,
                dirty=False,
            )
            previous.state = state
            previous.tool_name = tool_name
            if device_id is not None:
                previous.device_id = device_id
            previous.cwd = cwd
            previous.project = project
            previous.provider = provider
            previous.updated_at = now
            previous.dirty = True
            return previous, prev_snapshot

        entry = PresenceEntry(
            session_id=session_id,
            state=state,
            tool_name=tool_name,
            device_id=device_id,
            cwd=cwd,
            project=project,
            provider=provider,
            updated_at=now,
            dirty=True,
        )
        self._entries[session_id] = entry
        return entry, None

    # -- read path (called from _load_presence_map) --

    def get(self, session_id: str) -> PresenceEntry | None:
        return self._entries.get(session_id)

    def get_many(self, session_ids: list[str]) -> dict[str, PresenceEntry]:
        return {sid: self._entries[sid] for sid in session_ids if sid in self._entries}

    def to_presence_obj(self, entry: PresenceEntry) -> SessionPresence:
        """Convert cache entry to a SessionPresence-like object for API compat."""
        obj = SessionPresence()
        obj.session_id = entry.session_id
        obj.state = entry.state
        obj.tool_name = entry.tool_name
        obj.device_id = entry.device_id
        obj.cwd = entry.cwd
        obj.project = entry.project
        obj.provider = entry.provider
        obj.updated_at = entry.updated_at
        return obj

    # -- lifecycle --

    def warm_from_db(self, rows: list[SessionPresence]) -> None:
        """Load existing presence rows on startup (before flush loop starts)."""
        for row in rows:
            self._entries[row.session_id] = PresenceEntry(
                session_id=row.session_id,
                state=row.state,
                tool_name=row.tool_name,
                device_id=getattr(row, "device_id", None),
                cwd=row.cwd,
                project=row.project,
                provider=row.provider or "claude",
                updated_at=row.updated_at or datetime.now(timezone.utc),
                dirty=False,
            )
        self._cold = False

    @property
    def is_cold(self) -> bool:
        return self._cold

    def start_flush_loop(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    def stop_flush_loop(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
                await self._flush_dirty()
            except asyncio.CancelledError:
                # Final flush on shutdown
                await self._flush_dirty()
                return
            except Exception:
                logger.exception("Presence flush failed")

    async def _flush_dirty(self) -> None:
        dirty = [e for e in self._entries.values() if e.dirty]
        if not dirty:
            return

        from zerg.services.write_serializer import get_write_serializer

        ws = get_write_serializer()
        if not ws.is_configured:
            return  # startup race — skip silently

        def _do_flush(db):
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            for entry in dirty:
                stmt = (
                    sqlite_insert(SessionPresence)
                    .values(
                        session_id=entry.session_id,
                        state=entry.state,
                        tool_name=entry.tool_name,
                        device_id=entry.device_id,
                        cwd=entry.cwd,
                        project=entry.project,
                        provider=entry.provider,
                        updated_at=entry.updated_at,
                    )
                    .on_conflict_do_update(
                        index_elements=["session_id"],
                        set_={
                            "state": entry.state,
                            "tool_name": entry.tool_name,
                            "device_id": entry.device_id,
                            "cwd": entry.cwd,
                            "project": entry.project,
                            "updated_at": entry.updated_at,
                        },
                    )
                )
                db.execute(stmt)
                entry.dirty = False
            # auto_commit=True by default, so serializer commits

        try:
            await ws.execute(_do_flush, label="presence-flush")
            logger.debug("Flushed %d presence entries to DB", len(dirty))
        except Exception:
            logger.exception("Failed to flush %d presence entries", len(dirty))


# Process singleton
_cache = PresenceCache()


def get_presence_cache() -> PresenceCache:
    return _cache
