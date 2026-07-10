"""Cross-process admission signal for the Runtime Host monolith writer."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from zerg.services.archive_worker_status import archive_worker_status_path


def archive_api_writer_status_path() -> Path | None:
    explicit = os.getenv("LONGHOUSE_ARCHIVE_API_WRITER_STATUS_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    worker_path = archive_worker_status_path()
    if worker_path is None:
        return None
    return worker_path.with_name("archive-api-writer-status.json")


def write_archive_api_writer_status(payload: dict[str, Any]) -> None:
    path = archive_api_writer_status_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "pid": os.getpid(),
        "observed_at_unix": time.time(),
        **payload,
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def archive_api_writer_busy() -> bool:
    path = archive_api_writer_status_path()
    if path is None:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not payload.get("active"):
        return False
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
