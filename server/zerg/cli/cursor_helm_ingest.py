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
from zerg.utils.log import BestEffortLogger

_CHAT_DIR_ENV = "LH_CURSOR_HELM_CHAT_DIR"
_POLL_SECONDS_ENV = "LH_CURSOR_HELM_INGEST_POLL_SECONDS"
_DEFAULT_POLL_SECONDS = 3.0
_DISCOVERY_GRACE_SECONDS = 30.0
_INGEST_TIMEOUT = 30.0
_PROVIDER = "cursor"


class CursorHelmIngestRejected(RuntimeError):
    """Permanent Runtime Host rejection; retrying the same payload cannot help."""


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
        # Birthtime is the true "chat dir created" signal on macOS. Linux lacks
        # it; use mtime there because ctime changes on metadata updates such as
        # utime/chmod and can make an old store look newly created.
        dir_born = getattr(st_dir, "st_birthtime", None) or st_dir.st_mtime
        db_born = getattr(st_db, "st_birthtime", None) or st_db.st_mtime
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


def _post_delta(url: str, token: str, payload: "SessionIngest") -> bool:
    """POST one delta to the Runtime Host ingest endpoint.

    Returns True only on a 2xx acceptance. A 4xx (e.g. 422 validation
    rejection) is a real failure — treating it as success would advance the
    high-water mark and silently drop the rejected events forever, so we
    return False and let the next poll retry (giving the operator a chance to
    notice via verbose logs). 5xx and transport errors are also False.
    """
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
    if 200 <= resp.status_code < 300:
        return True
    if 400 <= resp.status_code < 500 and resp.status_code != 429:
        body = resp.text.strip()[:1000]
        raise CursorHelmIngestRejected(f"HTTP {resp.status_code}: {body or '<empty response>'}")
    return False


def run_transcript_tailer(
    *,
    store_db_path: Path,
    session_id: str,
    url: str,
    token: str,
    stop_event: threading.Event,
    poll_seconds: float | None = None,
    verbose: bool = False,
    bf: BestEffortLogger | None = None,
) -> None:
    """Stream new turn events from the cursor store.db to the Runtime Host.

    Runs until ``stop_event`` is set (child exit / terminate). Best-effort:
    decode/post errors are swallowed and retried on the next poll. Advances the
    high-water mark only after a successful (2xx) post, so transient failures
    retry without dropping events.

    Pass a shared ``bf`` (BestEffortLogger) so the caller can emit an end-of-run
    ingest summary at session exit. If omitted, a fresh one is created.
    """
    if bf is None:
        bf = BestEffortLogger("zerg.cursor_helm.ingest")
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
                        bf.success()
                        if verbose:
                            print(
                                f"longhouse cursor: shipped {len(new_events)} new event(s) " f"({hwm} total) to session {session_id}",
                                flush=True,
                            )
                    else:
                        # Non-2xx: server rejected the delta. Treat as a
                        # best-effort failure so it is logged rate-limited and
                        # counted in ingest health, then retry without
                        # advancing the high-water mark.
                        bf.failure("ingest post rejected (non-2xx)", RuntimeError("non-2xx response"))
        except CursorHelmIngestRejected as exc:
            # A typed 4xx is a contract mismatch, not transient churn. Log the
            # server response once and stop the retry loop instead of printing
            # an opaque warning every poll forever.
            bf.failure("ingest permanently rejected; tailer stopped", exc)
            return
        except Exception as exc:  # noqa: BLE001 - best-effort tailer must not die
            # Best-effort tailer: never let a decode/post/build error kill the
            # thread. The next poll retries. BestEffortLogger surfaces the
            # failure (rate-limited, at WARNING) so a silent crash — e.g. a
            # missing-config import raising RuntimeError every poll — is
            # visible without --verbose, not buried in `except: pass`.
            bf.failure("transcript poll", exc)
        stop_event.wait(poll_seconds)


def probe_runtime_ingest_compatibility(url: str, token: str) -> tuple[bool, str | None]:
    """Detect the known legacy-Cursor-vs-storage-v2 contract mismatch.

    Older Runtime Hosts may not expose the capabilities route; those still
    accept the legacy ingest path, so a 404 keeps the tailer enabled. Network
    and 5xx failures are transient and are left to normal retry behavior.
    """
    endpoint = f"{url.rstrip('/')}/api/agents/storage/v2/capabilities"
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(endpoint, headers={"X-Agents-Token": token})
    except httpx.HTTPError:
        return True, None
    if response.status_code == 404 or response.status_code >= 500:
        return True, None
    if response.status_code in {401, 403}:
        return False, f"Runtime Host rejected machine auth (HTTP {response.status_code})"
    if response.status_code != 200:
        return True, None
    try:
        capabilities = response.json()
    except ValueError:
        return True, None
    if capabilities.get("cutover") is True:
        return (
            False,
            "Runtime Host requires storage-v2, but Cursor Helm live transcript tailing still uses legacy ingest",
        )
    return True, None


def probe_ingest_path() -> tuple[bool, str | None]:
    """Exercise the exact import + model-construction path the tailer uses on
    every poll, without needing a real ``store.db``.

    Returns ``(ok, error)``. This catches the class of bug that silently killed
    Helm transcript ingest for weeks — a transitive import (e.g.
    ``zerg.database`` → ``get_settings()`` → ``_validate_required()``) raising
    at call time when ``DATABASE_URL`` is unset on a remote-only CLI. The
    tailer's best-effort ``except`` would swallow that crash on every poll; the
    probe surfaces it at launch instead of after the first turn, so the user
    knows the session will be steerable but blind rather than discovering it
    from an empty timeline 30 minutes in.
    """
    try:
        from zerg.services.agents.models import EventIngest

        ev = EventIngest(role="system", content_text="", timestamp=datetime.now(timezone.utc))
        SessionIngest(
            provider="cursor",
            environment="production",
            started_at=datetime.now(timezone.utc),
            events=[ev],
        )
        return True, None
    except Exception as exc:  # noqa: BLE001 - probe must not crash launch
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def run_helm_ingest_thread(
    *,
    launch_time: datetime,
    session_id: str,
    url: str,
    token: str,
    stop_event: threading.Event,
    verbose: bool = False,
    bf: BestEffortLogger | None = None,
) -> None:
    """Discover this session's cursor store.db, then stream its transcript.

    Polls for the chat dir cursor-agent creates after launch (or honors
    ``LH_CURSOR_HELM_CHAT_DIR``), then hands off to ``run_transcript_tailer``.
    Safe to run as a daemon thread; exits when ``stop_event`` is set.

    Pass a shared ``bf`` so the launcher can emit an end-of-run ingest summary.
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
        bf=bf,
    )
