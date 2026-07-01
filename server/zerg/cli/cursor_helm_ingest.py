"""Live transcript tailer for Cursor Helm sessions.

The Helm launcher (``zerg.cli.cursor_helm``) owns a managed ``cursor-agent``
session but, left to itself, ships no transcript to the timeline as turns
commit — the engine has no ``~/.cursor`` tailer, so a Helm session would be
steerable but blind. This module runs a background thread in the launcher that
streams new turn events to the Runtime Host as cursor writes them.

Mechanism (see ``docs/specs/cursor-live-ingest.md``):

- Cursor's ``store.db`` is a content-addressed blob DAG; the root snapshot's
  message list is append-only (new turns append new blobs, old blob ids
  persist). Decode order is therefore stable across decodes of the same store.
- Each poll we re-decode the store and assign every event a stable
  ``source_offset`` = its global ordinal in decode order. We ship only events
  with ``ordinal >= high_water_mark``. Each event is shipped exactly once, so
  its synthesized timestamp is computed once and never shifts — the duplicate
  problem that a naive re-post-everything tailer would hit does not arise.
- The payload is a ``SessionIngest`` with ``id = <managed session id>`` so
  events bind to the managed Helm session, and ``/api/agents/ingest`` dedupes
  by ``(source_path, source_offset)`` as a backstop.

This is the Helm-only seam. Shadow (unmanaged) live ingest is a separate,
deferred engine-Rust path that benefits all unmanaged cursor sessions.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import UUID

import httpx

from zerg.services.agents.models import SessionIngest
from zerg.services.cursor_transcript import decode_store_db
from zerg.services.cursor_transcript import iter_local_cursor_stores

_CHAT_DIR_ENV = "LH_CURSOR_HELM_CHAT_DIR"
_POLL_SECONDS_ENV = "LH_CURSOR_HELM_INGEST_POLL_SECONDS"
_DEFAULT_POLL_SECONDS = 3.0
_DISCOVERY_GRACE_SECONDS = 30.0
_INGEST_TIMEOUT = 30.0
_PROVIDER = "cursor"


def _cursor_chats_root() -> Path:
    return Path.home() / ".cursor" / "chats"


def _store_db_for_override(override: str) -> Path | None:
    p = Path(override).expanduser()
    if p.is_dir():
        p = p / "store.db"
    return p if p.exists() else None


def discover_store_db(
    launch_time: datetime,
    *,
    now: datetime | None = None,
    override: str | None = None,
    cursor_root: Path | None = None,
) -> Path | None:
    """Find the store.db for this Helm session.

    Resolution order:
    1. ``LH_CURSOR_HELM_CHAT_DIR`` / ``override`` env — point at the exact chat
       dir for deterministic dogfood.
    2. Otherwise scan ``~/.cursor/chats/*/store.db`` for the newest one whose
       parent dir was created around or after ``launch_time`` (cursor creates a
       fresh chat dir per session). Returns None until that store appears.
    """
    if override is None:
        override = os.environ.get(_CHAT_DIR_ENV, "").strip() or None
    if override:
        return _store_db_for_override(override)

    now = now or datetime.now(timezone.utc)
    window_start = launch_time.timestamp() - _DISCOVERY_GRACE_SECONDS
    root = cursor_root or _cursor_chats_root()
    newest: Path | None = None
    newest_mtime = -1.0
    for store_path in iter_local_cursor_stores(root):
        try:
            st_dir = os.stat(store_path.parent)
            st_db = os.stat(store_path)
        except OSError:
            continue
        # Birthtime is the true "chat dir created" signal on macOS; fall back to
        # ctime/mtime where unavailable.
        dir_born = getattr(st_dir, "st_birthtime", None) or st_dir.st_ctime
        db_born = getattr(st_db, "st_birthtime", None) or st_db.st_ctime
        created = max(dir_born, db_born)
        if created < window_start:
            continue
        if st_db.st_mtime > newest_mtime:
            newest_mtime = st_db.st_mtime
            newest = store_path
    return newest


def _build_delta_payload(
    session_id: str,
    decoded_session: SessionIngest,
    events: list,
    hwm: int,
) -> tuple[SessionIngest, list] | None:
    """Return (payload, new_events) for events[ordinal >= hwm], or None if
    there is nothing new to ship. Stamps each shipped event with a stable
    ordinal source_offset so ingest dedupes by (source_path, source_offset)."""
    new_events: list = []
    for ordinal, ev in enumerate(events):
        if ordinal < hwm:
            continue
        # source_offset is Optional[int] on EventIngest; the cursor decoder
        # leaves it None. A stable per-event ordinal makes ingest idempotent.
        ev.source_offset = ordinal
        new_events.append(ev)
    if not new_events:
        return None
    payload = SessionIngest(
        id=UUID(session_id),
        provider=decoded_session.provider,
        environment=decoded_session.environment,
        project=decoded_session.project,
        device_id=decoded_session.device_id,
        device_name=decoded_session.device_name,
        cwd=decoded_session.cwd,
        git_repo=decoded_session.git_repo,
        git_branch=decoded_session.git_branch,
        started_at=decoded_session.started_at,
        ended_at=decoded_session.ended_at,
        provider_session_id=decoded_session.provider_session_id,
        events=new_events,
    )
    return payload, new_events


def _post_delta(url: str, token: str, payload: SessionIngest) -> bool:
    endpoint = f"{url.rstrip('/')}/api/agents/ingest"
    try:
        with httpx.Client(timeout=_INGEST_TIMEOUT) as client:
            resp = client.post(
                endpoint,
                headers={"X-Agents-Token": token, "Content-Type": "application/json"},
                content=payload.model_dump_json(),
            )
    except httpx.HTTPError:
        return False
    return resp.status_code < 500


def run_transcript_tailer(
    *,
    store_db_path: Path,
    session_id: str,
    url: str,
    token: str,
    stop_event: threading.Event,
    poll_seconds: float | None = None,
    verbose: bool = False,
) -> None:
    """Stream new turn events from the cursor store.db to the Runtime Host.

    Runs until ``stop_event`` is set (child exit / terminate). Best-effort:
    decode/post errors are swallowed and retried on the next poll. Advances the
    high-water mark only after a successful (2xx) post, so transient failures
    retry without dropping events.
    """
    if poll_seconds is None:
        raw = os.environ.get(_POLL_SECONDS_ENV, "").strip()
        try:
            poll_seconds = float(raw) if raw else _DEFAULT_POLL_SECONDS
        except ValueError:
            poll_seconds = _DEFAULT_POLL_SECONDS

    hwm = 0
    while not stop_event.is_set():
        try:
            decoded = decode_store_db(store_db_path)
            if decoded.session is not None and decoded.session.events:
                built = _build_delta_payload(session_id, decoded.session, decoded.session.events, hwm)
                if built is not None:
                    payload, new_events = built
                    if _post_delta(url, token, payload):
                        hwm += len(new_events)
                        if verbose:
                            shipped_total = hwm
                            print(
                                f"longhouse cursor: shipped {len(new_events)} new event(s) "
                                f"({shipped_total} total) to session {session_id}",
                                flush=True,
                            )
        except Exception:
            # Best-effort tailer: never let a decode/post error kill the thread.
            # The next poll retries.
            pass
        stop_event.wait(poll_seconds)


def run_helm_ingest_thread(
    *,
    launch_time: datetime,
    session_id: str,
    url: str,
    token: str,
    stop_event: threading.Event,
    verbose: bool = False,
) -> None:
    """Discover this session's cursor store.db, then stream its transcript.

    Polls for the chat dir cursor-agent creates after launch (or honors
    ``LH_CURSOR_HELM_CHAT_DIR``), then hands off to ``run_transcript_tailer``.
    Safe to run as a daemon thread; exits when ``stop_event`` is set.
    """
    poll_seconds = _DEFAULT_POLL_SECONDS
    raw = os.environ.get(_POLL_SECONDS_ENV, "").strip()
    try:
        poll_seconds = float(raw) if raw else _DEFAULT_POLL_SECONDS
    except ValueError:
        pass

    store_db: Path | None = None
    while not stop_event.is_set() and store_db is None:
        store_db = discover_store_db(launch_time)
        if store_db is None:
            stop_event.wait(poll_seconds)

    if store_db is None:
        return  # stop_event set before cursor wrote a store

    if verbose:
        print(f"longhouse cursor: tailing transcript at {store_db}", flush=True)

    run_transcript_tailer(
        store_db_path=store_db,
        session_id=session_id,
        url=url,
        token=token,
        stop_event=stop_event,
        verbose=verbose,
    )
