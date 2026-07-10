"""Process-isolated LiveArchiveOutbox drain worker."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from zerg.services.archive_worker_status import archive_worker_lock_path
from zerg.services.archive_worker_status import write_archive_worker_status

logger = logging.getLogger(__name__)


def _drain_interval_seconds() -> float:
    return max(0.05, float(os.getenv("LONGHOUSE_ARCHIVE_WORKER_INTERVAL_SECONDS", "1")))


@contextmanager
def _exclusive_worker_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"archive worker lock is already held: {path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def worker_owned_outbox_kinds() -> set[str]:
    from zerg.services.live_archive_outbox import HEARTBEAT_STAMP_KIND
    from zerg.services.live_archive_outbox import MANAGED_LOCAL_LAUNCH_KIND
    from zerg.services.live_archive_outbox import REMOTE_LAUNCH_KIND
    from zerg.services.live_archive_outbox import REMOTE_LAUNCH_OUTCOME_KIND
    from zerg.services.live_archive_outbox import RUNTIME_EVENT_KIND
    from zerg.services.live_archive_outbox import SESSION_INPUT_RECEIPT_KIND

    return {
        HEARTBEAT_STAMP_KIND,
        MANAGED_LOCAL_LAUNCH_KIND,
        REMOTE_LAUNCH_KIND,
        REMOTE_LAUNCH_OUTCOME_KIND,
        RUNTIME_EVENT_KIND,
        SESSION_INPUT_RECEIPT_KIND,
    }


def _next_pending_outbox_row(live_session_factory) -> dict[str, object] | None:
    from zerg.models.live_store import LiveArchiveOutbox

    with live_session_factory() as live_db:
        row = (
            live_db.query(LiveArchiveOutbox.id)
            .filter(LiveArchiveOutbox.drained_at.is_(None))
            .filter(LiveArchiveOutbox.kind.in_(sorted(worker_owned_outbox_kinds())))
            .order_by(LiveArchiveOutbox.attempts.asc(), LiveArchiveOutbox.created_at.asc(), LiveArchiveOutbox.id.asc())
            .first()
        )
        if row is None:
            return None
        full_row = live_db.get(LiveArchiveOutbox, int(row.id))
        if full_row is None:
            return None
        return {
            "id": int(full_row.id),
            "kind": str(full_row.kind),
            "payload_json": str(full_row.payload_json),
        }


def _record_outbox_failure(live_session_factory, *, row_id: int, error: str) -> None:
    from zerg.models.live_store import LiveArchiveOutbox

    with live_session_factory() as live_db:
        row = live_db.get(LiveArchiveOutbox, row_id)
        if row is None or row.drained_at is not None:
            return
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = error
        live_db.commit()


def _acknowledge_outbox_row(
    live_session_factory,
    *,
    envelope: dict[str, object],
    effects: dict[str, object],
) -> bool:
    from datetime import datetime
    from datetime import timezone

    from zerg.models.live_store import LiveArchiveOutbox
    from zerg.services.live_archive_outbox import apply_live_archive_outbox_ack

    with live_session_factory() as live_db:
        row = live_db.get(LiveArchiveOutbox, int(envelope["id"]))
        if row is None or row.drained_at is not None:
            return False
        apply_live_archive_outbox_ack(row, live_db, effects)
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = None
        row.drained_at = datetime.now(timezone.utc)
        live_db.commit()
        return True


def drain_once(*, limit: int | None = None) -> dict[str, int]:
    from zerg.database import get_live_session_factory
    from zerg.database import get_write_session_factory
    from zerg.services.live_archive_outbox import apply_live_archive_outbox_to_archive

    live_session_factory = get_live_session_factory()
    archive_session_factory = get_write_session_factory()
    if live_session_factory is None or archive_session_factory is None:
        raise RuntimeError("archive worker requires live and archive session factories")

    envelope = _next_pending_outbox_row(live_session_factory)
    if envelope is None:
        return {"processed": 0, "drained": 0, "failed": 0}

    if os.getenv("LONGHOUSE_ARCHIVE_WORKER_TEST_EXIT_BEFORE_DRAIN") == "1":
        os._exit(91)

    del limit  # The worker owns one outbox row per cold transaction.
    try:
        with archive_session_factory() as archive_db:
            detached_row = SimpleNamespace(
                id=envelope["id"],
                kind=envelope["kind"],
                payload_json=envelope["payload_json"],
            )
            effects = apply_live_archive_outbox_to_archive(detached_row, archive_db)
            archive_db.commit()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _record_outbox_failure(live_session_factory, row_id=int(envelope["id"]), error=error)
        return {"processed": 1, "drained": 0, "failed": 1}

    try:
        drained = _acknowledge_outbox_row(live_session_factory, envelope=envelope, effects=effects)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _record_outbox_failure(live_session_factory, row_id=int(envelope["id"]), error=error)
        return {"processed": 1, "drained": 0, "failed": 1}
    return {"processed": 1, "drained": int(drained), "failed": int(not drained)}


def run_worker(*, once: bool = False) -> int:
    lock_path = archive_worker_lock_path()
    if lock_path is None:
        raise RuntimeError("archive worker requires a file-backed live database or explicit status path")

    stop_requested = False

    def _request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    with _exclusive_worker_lock(lock_path):
        started_at = time.time()
        totals = {"processed": 0, "drained": 0, "failed": 0}
        consecutive_failures = 0
        write_archive_worker_status(
            {
                "status": "running",
                "pid": os.getpid(),
                "started_at_unix": started_at,
                "consecutive_failures": 0,
                **totals,
            }
        )
        while not stop_requested:
            try:
                result = drain_once()
                for key in totals:
                    totals[key] += int(result.get(key, 0))
                consecutive_failures = 0 if not result.get("failed") else consecutive_failures + 1
                write_archive_worker_status(
                    {
                        "status": "running",
                        "pid": os.getpid(),
                        "started_at_unix": started_at,
                        "consecutive_failures": consecutive_failures,
                        "last_result": result,
                        **totals,
                    }
                )
            except Exception as exc:
                consecutive_failures += 1
                logger.exception("Archive worker drain failed")
                write_archive_worker_status(
                    {
                        "status": "degraded",
                        "pid": os.getpid(),
                        "started_at_unix": started_at,
                        "consecutive_failures": consecutive_failures,
                        "last_error": f"{type(exc).__name__}: {exc}",
                        **totals,
                    }
                )
            if once:
                break
            if result.get("processed") and not result.get("failed"):
                continue
            time.sleep(_drain_interval_seconds())

        write_archive_worker_status(
            {
                "status": "stopped",
                "pid": os.getpid(),
                "started_at_unix": started_at,
                "consecutive_failures": consecutive_failures,
                **totals,
            }
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    return run_worker(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
