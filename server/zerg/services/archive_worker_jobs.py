"""Durable filesystem jobs for synchronous cold work owned by the archive worker."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from typing import Callable
from uuid import uuid4

from zerg.services.archive_worker_status import archive_worker_status_path


class ArchiveWorkerJobError(RuntimeError):
    pass


class ArchiveWorkerJobTimeout(ArchiveWorkerJobError):
    pass


_JOB_PRIORITY = {
    "ingest-live": 0,
    "ingest": 10,
    "ingest-scan": 20,
    "ingest-replay": 30,
}


def archive_worker_jobs_root() -> Path:
    status_path = archive_worker_status_path()
    if status_path is None:
        raise RuntimeError("archive worker jobs require a file-backed worker status path")
    return status_path.parent / "archive-worker-jobs"


def _job_dirs() -> tuple[Path, Path, Path]:
    root = archive_worker_jobs_root()
    pending = root / "pending"
    running = root / "running"
    results = root / "results"
    for path in (pending, running, results):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return pending, running, results


def archive_worker_job_counts() -> dict[str, int]:
    pending, running, results = _job_dirs()
    return {
        "jobs_pending": sum(1 for _ in pending.glob("*.json")),
        "jobs_running": sum(1 for _ in running.glob("*.json")),
        "job_results_waiting": sum(1 for _ in results.glob("*.json")),
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _durable_replace(source: Path, target: Path) -> None:
    os.replace(source, target)
    _fsync_dir(source.parent)
    if target.parent != source.parent:
        _fsync_dir(target.parent)


async def submit_archive_worker_job(
    kind: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    pending, _running, results = _job_dirs()
    max_pending = 1000
    if sum(1 for _ in pending.glob("*.json")) >= max_pending:
        raise ArchiveWorkerJobError(f"archive worker pending-job limit reached ({max_pending})")
    job_id = str(uuid4())
    write_label = str(payload.get("write_label") or "ingest")
    priority = _JOB_PRIORITY.get(write_label, 50)
    request_path = pending / f"{priority:03d}-{time.time_ns():020d}-{job_id}.json"
    result_path = results / f"{job_id}.json"
    submitted_at = time.monotonic()
    await asyncio.to_thread(
        _atomic_json,
        request_path,
        {
            "schema_version": 1,
            "job_id": job_id,
            "kind": kind,
            "priority": priority,
            "created_at_unix": time.time(),
            "payload": payload,
        },
    )

    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
    while asyncio.get_running_loop().time() < deadline:
        try:
            result_payload = await asyncio.to_thread(_read_and_remove_result, result_path)
        except FileNotFoundError:
            await asyncio.sleep(0.05)
            continue
        if result_payload.get("ok"):
            result = result_payload.get("result")
            response = result if isinstance(result, dict) else {}
            response["job_wait_ms"] = round((time.monotonic() - submitted_at) * 1000, 1)
            return response
        error = result_payload.get("error") or {}
        error_type = error.get("type") or "ArchiveWorkerError"
        error_message = error.get("message") or "job failed"
        raise ArchiveWorkerJobError(f"{error_type}: {error_message}")
    raise ArchiveWorkerJobTimeout(f"archive worker job {job_id} exceeded {timeout_seconds:.1f}s")


def _read_and_remove_result(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.unlink(missing_ok=True)
    _fsync_dir(path.parent)
    return payload


def recover_interrupted_archive_jobs() -> int:
    pending, running, _results = _job_dirs()
    recovered = 0
    for path in sorted(running.glob("*.json")):
        target = pending / path.name
        if target.exists():
            path.unlink()
            _fsync_dir(path.parent)
        else:
            _durable_replace(path, target)
        recovered += 1
    return recovered


def process_next_archive_worker_job(
    *,
    on_start: Callable[[dict[str, Any]], None] | None = None,
    allow_background: bool = True,
) -> bool:
    from zerg.services.archive_api_writer_status import archive_api_writer_busy

    if archive_api_writer_busy():
        return False
    pending, running, results = _job_dirs()
    request_paths = sorted(pending.glob("*.json"), key=lambda path: path.name)
    if not allow_background:
        request_paths = [path for path in request_paths if path.name.startswith("000-")]
    if not request_paths:
        return False
    request_path = request_paths[0]
    running_path = running / request_path.name
    try:
        _durable_replace(request_path, running_path)
    except FileNotFoundError:
        return False

    request = json.loads(running_path.read_text(encoding="utf-8"))
    if on_start is not None:
        on_start(request)
    if os.getenv("LONGHOUSE_ARCHIVE_WORKER_TEST_EXIT_BEFORE_JOB") == "1":
        os._exit(92)
    job_id = str(request.get("job_id") or running_path.stem)
    result_path = results / f"{job_id}.json"
    try:
        result = _execute_job(str(request.get("kind") or ""), request.get("payload") or {})
        response = {"schema_version": 1, "job_id": job_id, "ok": True, "result": result}
    except Exception as exc:
        response = {
            "schema_version": 1,
            "job_id": job_id,
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    _atomic_json(result_path, response)
    running_path.unlink(missing_ok=True)
    _fsync_dir(running_path.parent)
    return True


def prune_archive_worker_jobs(*, older_than_seconds: float = 3600.0) -> int:
    _pending, _running, results = _job_dirs()
    cutoff = time.time() - max(60.0, older_than_seconds)
    removed = 0
    # Pending and running requests must survive until processed or explicitly
    # recovered. Only orphaned results are safe to expire.
    for path in results.glob("*.json"):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    if removed:
        _fsync_dir(results)
    return removed


def _execute_job(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == "session_ingest.v1":
        from zerg.services.archive_ingest_job import execute_archive_ingest_job

        return execute_archive_ingest_job(payload)
    raise ValueError(f"unsupported archive worker job kind: {kind}")
