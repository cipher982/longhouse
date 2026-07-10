"""Process-isolated LiveArchiveOutbox drain worker."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from zerg.services.archive_worker_status import archive_worker_lock_path
from zerg.services.archive_worker_status import write_archive_worker_status

logger = logging.getLogger(__name__)


class _StatusReporter:
    """Keep liveness fresh even while the worker is blocked in cold SQLite."""

    def __init__(self, *, started_at: float) -> None:
        self.started_at = started_at
        self._payload: dict[str, object] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="archive-worker-status", daemon=True)

    def start(self, payload: dict[str, object]) -> None:
        self.update(payload)
        self._thread.start()

    def update(self, payload: dict[str, object]) -> None:
        with self._lock:
            self._payload = dict(payload)
        self._write()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _write(self) -> None:
        with self._lock:
            payload = dict(self._payload)
        write_archive_worker_status({"pid": os.getpid(), "started_at_unix": self.started_at, **payload})

    def _run(self) -> None:
        while not self._stop.wait(2.0):
            self._write()


def _drain_interval_seconds() -> float:
    return max(0.05, float(os.getenv("LONGHOUSE_ARCHIVE_WORKER_INTERVAL_SECONDS", "1")))


def _busy_yield_seconds() -> float:
    # SQLite coordinates writers across processes, but an immediate worker
    # re-claim can repeatedly beat the API writer after every short commit.
    # Yield a bounded slice so ingest/catalog work gets an acquisition window.
    return max(0.0, float(os.getenv("LONGHOUSE_ARCHIVE_WORKER_BUSY_YIELD_SECONDS", "0.05")))


def _select_work_once(*, prefer_jobs: bool, process_job, drain_outbox):
    """Alternate durable jobs and outbox work while either lane is busy."""

    job_processed = False
    outbox_result = {"processed": 0, "drained": 0, "failed": 0}
    if prefer_jobs:
        job_processed = process_job()
        if not job_processed:
            outbox_result = drain_outbox()
    else:
        outbox_result = drain_outbox()
        if not outbox_result.get("processed"):
            job_processed = process_job()
    next_prefer_jobs = False if job_processed else bool(outbox_result.get("processed"))
    return job_processed, outbox_result, next_prefer_jobs


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


def drain_once(
    *,
    limit: int | None = None,
    on_start=None,
) -> dict[str, int]:
    from zerg.services.archive_api_writer_status import archive_api_writer_busy

    if archive_api_writer_busy():
        return {"processed": 0, "drained": 0, "failed": 0}

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
    if on_start is not None:
        on_start(envelope)

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
    from zerg.services.archive_worker_jobs import archive_worker_job_counts
    from zerg.services.archive_worker_jobs import process_next_archive_worker_job
    from zerg.services.archive_worker_jobs import prune_archive_worker_jobs
    from zerg.services.archive_worker_jobs import recover_interrupted_archive_jobs

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
        recovered_jobs = recover_interrupted_archive_jobs()
        prune_archive_worker_jobs()
        totals = {"processed": 0, "drained": 0, "failed": 0, "jobs_processed": 0}
        consecutive_failures = 0
        reporter = _StatusReporter(started_at=started_at)
        reporter.start(
            {
                "status": "running",
                "consecutive_failures": 0,
                "recovered_jobs": recovered_jobs,
                "restart_count": int(os.getenv("LONGHOUSE_ARCHIVE_WORKER_RESTART_COUNT", "0")),
                "restart_backoff_seconds": float(os.getenv("LONGHOUSE_ARCHIVE_WORKER_RESTART_BACKOFF_SECONDS", "1")),
                **totals,
            }
        )
        result: dict[str, int] = {"processed": 0, "drained": 0, "failed": 0}
        prefer_jobs = True

        def report_active(operation: str, identity: object) -> None:
            reporter.update(
                {
                    "status": "running",
                    "consecutive_failures": consecutive_failures,
                    "active_operation": operation,
                    "active_identity": str(identity),
                    "active_started_at_unix": time.time(),
                    **archive_worker_job_counts(),
                    **totals,
                }
            )

        try:
            while not stop_requested:
                try:
                    job_processed, outbox_result, prefer_jobs = _select_work_once(
                        prefer_jobs=prefer_jobs,
                        process_job=lambda: process_next_archive_worker_job(
                            on_start=lambda job: report_active("job", job.get("job_id")),
                        ),
                        drain_outbox=lambda: drain_once(
                            on_start=lambda row: report_active("outbox", row.get("id")),
                        ),
                    )
                    if job_processed:
                        totals["jobs_processed"] += 1
                        result = {"processed": 1, "drained": 0, "failed": 0}
                    else:
                        result = outbox_result
                    for key in ("processed", "drained", "failed"):
                        totals[key] += int(result.get(key, 0))
                    consecutive_failures = 0 if not result.get("failed") else consecutive_failures + 1
                    reporter.update(
                        {
                            "status": "running",
                            "consecutive_failures": consecutive_failures,
                            "last_result": result,
                            "last_progress_at_unix": time.time() if result.get("processed") else None,
                            "active_operation": None,
                            "active_identity": None,
                            "active_started_at_unix": None,
                            **archive_worker_job_counts(),
                            **totals,
                        }
                    )
                except Exception as exc:
                    consecutive_failures += 1
                    logger.exception("Archive worker iteration failed")
                    result = {"processed": 0, "drained": 0, "failed": 1}
                    reporter.update(
                        {
                            "status": "degraded",
                            "consecutive_failures": consecutive_failures,
                            "last_error": f"{type(exc).__name__}: {exc}",
                            **totals,
                        }
                    )
                if once:
                    break
                if result.get("processed") and not result.get("failed"):
                    time.sleep(_busy_yield_seconds())
                    continue
                time.sleep(_drain_interval_seconds())
        finally:
            reporter.update(
                {
                    "status": "stopped",
                    "consecutive_failures": consecutive_failures,
                    **totals,
                }
            )
            reporter.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    return run_worker(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
