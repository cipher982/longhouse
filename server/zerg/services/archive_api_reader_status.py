"""Cross-process activity signal for user-facing archive API readers."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from zerg.services.archive_worker_status import archive_worker_status_path

_STALE_AFTER_SECONDS = 120.0
_state_lock = threading.Lock()
_state_pid = os.getpid()
_active_count = 0


def archive_api_reader_status_root() -> Path | None:
    worker_path = archive_worker_status_path()
    if worker_path is None:
        return None
    return worker_path.with_name("archive-api-readers")


def _status_path(pid: int | None = None) -> Path | None:
    root = archive_api_reader_status_root()
    if root is None:
        return None
    return root / f"{pid or os.getpid()}.json"


def _write_status(*, pid: int, active_count: int) -> None:
    path = _status_path(pid)
    if path is None:
        return
    if active_count <= 0:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "pid": pid,
        "active_count": active_count,
        "observed_at_unix": time.time(),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _change_active_count(delta: int) -> None:
    global _active_count, _state_pid

    pid = os.getpid()
    with _state_lock:
        # Do not inherit the parent's in-memory count if this process was
        # forked while a request was active.
        if _state_pid != pid:
            _state_pid = pid
            _active_count = 0
        _active_count = max(0, _active_count + delta)
        _write_status(pid=pid, active_count=_active_count)


@contextmanager
def archive_api_reader_activity(*, enabled: bool = True) -> Iterator[None]:
    """Publish one active user read until the request scope unwinds.

    ``enabled=False`` lets callers keep background archive traffic out of the
    user-read pressure signal without duplicating request-scope control flow.
    """

    if not enabled:
        yield
        return
    _change_active_count(1)
    try:
        yield
    finally:
        _change_active_count(-1)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def archive_api_reader_busy(*, now: float | None = None) -> bool:
    """Return whether any live Runtime Host process reports a user read."""

    root = archive_api_reader_status_root()
    if root is None:
        return False
    observed_now = time.time() if now is None else now
    try:
        paths = tuple(root.glob("*.json"))
    except OSError:
        return False
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid") or 0)
            active_count = int(payload.get("active_count") or 0)
            observed_at = float(payload.get("observed_at_unix") or 0.0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        fresh = 0.0 <= observed_now - observed_at <= _STALE_AFTER_SECONDS
        if active_count > 0 and fresh and _pid_is_alive(pid):
            return True
        try:
            path.unlink()
        except OSError:
            pass
    return False
