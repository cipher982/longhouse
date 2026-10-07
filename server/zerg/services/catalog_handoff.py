"""Warm candidate start: boot beside the serving process, take the catalog last.

A hosted cutover (control-plane ``runtime-host-update-continuity.md`` B2) starts
the candidate container while the predecessor still serves. This process does
everything that does not touch the tenant's data first (interpreter boot,
imports, app construction), then waits for two things in order:

1. **The permit.** The deployer writes ``<attempt>.permit`` into the handoff
   directory only after the predecessor has drained. Until then this process
   does not bind HTTP, so the edge keeps dialing the predecessor.
2. **The catalog lock.** The predecessor's catalogd releases the data-root lock
   as it exits. Only then does this process start its own catalogd.

One SQLite writer per tenant is enforced by catalogd's ``flock``; this module
never holds that lock itself, it only waits until it is free. The deployer
reads the markers this process writes (``warm``, ``bound``, ``catalog``) from
the same directory, which is the tenant's own data root, so no network address
is needed before the edge alias moves.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HANDOFF_DIR_ENV = "LONGHOUSE_CATALOG_HANDOFF_DIR"
_PERMIT_POLL_SECONDS = 0.02
_LOCK_POLL_SECONDS = 0.01
_BIND_POLL_SECONDS = 0.005
_BIND_TIMEOUT_SECONDS = 10.0
# Without a deployer cutoff a warm candidate still must not wait for ever.
_DEFAULT_PERMIT_WAIT_SECONDS = 960.0


class CatalogHandoffAborted(RuntimeError):
    """The permit or the lock did not arrive inside the attempt's horizon."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


@dataclass
class CatalogHandoff:
    directory: Path
    attempt_id: str
    runtime_epoch: str
    cutoff: datetime | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    failed: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    _started: float = field(default_factory=time.monotonic)

    def marker_path(self, name: str) -> Path:
        return self.directory / f"{self.attempt_id}.{name}"

    def _write_marker(self, name: str, payload: dict[str, Any]) -> None:
        body = {
            "attempt_id": self.attempt_id,
            "runtime_epoch": self.runtime_epoch,
            "pid": os.getpid(),
            "at": _utc_now(),
            "since_process_start_ms": round((time.monotonic() - self._started) * 1000, 1),
            **payload,
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.attempt_id}.{name}.", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, sort_keys=True, separators=(",", ":"))
            os.replace(temporary, self.marker_path(name))
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _mark(self, stage: str) -> None:
        self.timings[stage] = round((time.monotonic() - self._started) * 1000, 1)

    def _remaining_seconds(self) -> float:
        if self.cutoff is None:
            return _DEFAULT_PERMIT_WAIT_SECONDS - (time.monotonic() - self._started)
        return (self.cutoff - datetime.now(timezone.utc)).total_seconds()

    def _permit_matches(self) -> bool:
        return permit_matches(self.directory, self.attempt_id)

    def remaining_seconds(self) -> float:
        return self._remaining_seconds()

    async def wait_for_permit(self) -> None:
        """Announce that boot is done, then wait for the deployer's permit."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._mark("warm")
        self._write_marker("warm", {"state": "warm"})
        logger.info("catalog handoff warm; waiting for permit attempt_id=%s", self.attempt_id)
        while not self._permit_matches():
            if self._remaining_seconds() <= 0:
                raise CatalogHandoffAborted("catalog handoff permit did not arrive before the attempt cutoff")
            await asyncio.sleep(_PERMIT_POLL_SECONDS)
        self._mark("permit")
        logger.info("catalog handoff permitted attempt_id=%s", self.attempt_id)

    async def announce_bound(self, port: int) -> None:
        """Write ``bound`` once this process accepts connections on its port."""
        deadline = time.monotonic() + _BIND_TIMEOUT_SECONDS
        while True:
            try:
                _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=0.2)
                writer.close()
                break
            except (OSError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise CatalogHandoffAborted("runtime HTTP did not bind after the handoff permit")
                await asyncio.sleep(_BIND_POLL_SECONDS)
        self._mark("bound")
        self._write_marker("bound", {"state": "bound"})

    def mark_ready(self) -> None:
        self._mark("catalog_ready")
        self._write_marker("catalog", {"state": "catalog_ready", "timings_ms": dict(self.timings)})
        self.ready.set()
        logger.info("catalog handoff complete %s", json.dumps(self.timings, sort_keys=True))

    def mark_failed(self, detail: str) -> None:
        self.failed = detail
        try:
            self._write_marker("failed", {"state": "failed", "detail": detail[:500]})
        except OSError:
            logger.exception("Could not record catalog handoff failure")
        logger.error("catalog handoff failed: %s", detail)


def permit_matches(directory: Path, attempt_id: str) -> bool:
    try:
        raw = (directory / f"{attempt_id}.permit").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("attempt_id") == attempt_id


def lock_is_free(lock_path: Path) -> bool:
    """Probe the catalog lock on a private descriptor and never keep it.

    catalogd takes the lock itself with LOCK_NB on its own descriptor, so
    holding it here would make catalogd fail.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}:
                return False
            raise
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


def wait_for_handoff_from_env(lock_path: Path) -> None:
    """catalogd's side of a warm start: the permit first, then the free lock.

    Runs in the catalogd process, which the warm candidate spawns before the
    permit so interpreter boot and imports happen outside the closed-writes
    window. Nothing here touches the database; the predecessor's catalogd holds
    the lock until it exits, and only then may this process take it.
    """
    directory = Path(os.environ[HANDOFF_DIR_ENV])
    attempt_id = os.environ["LONGHOUSE_DEPLOYMENT_ATTEMPT_ID"].strip()
    cutoff = _parse_time(os.getenv("LONGHOUSE_CLAIM_CUTOFF"))
    deadline = time.monotonic() + _DEFAULT_PERMIT_WAIT_SECONDS
    if cutoff is not None:
        deadline = time.monotonic() + (cutoff - datetime.now(timezone.utc)).total_seconds()
    while not permit_matches(directory, attempt_id):
        if time.monotonic() >= deadline:
            raise CatalogHandoffAborted("catalog handoff permit did not arrive before the attempt cutoff")
        time.sleep(_PERMIT_POLL_SECONDS)
    while not lock_is_free(lock_path):
        if time.monotonic() >= deadline:
            raise CatalogHandoffAborted("catalog lock was not released before the attempt cutoff")
        time.sleep(_LOCK_POLL_SECONDS)


_HANDOFF: CatalogHandoff | None = None
_HANDOFF_LOADED = False


def catalog_handoff() -> CatalogHandoff | None:
    """The warm-start handoff for this process, or None for an ordinary start.

    Warm start needs a pending deployment (the candidate never serves writes
    before reopen) and an attempt id that names the permit.
    """
    global _HANDOFF, _HANDOFF_LOADED
    if _HANDOFF_LOADED:
        return _HANDOFF
    _HANDOFF_LOADED = True
    directory = os.getenv(HANDOFF_DIR_ENV, "").strip()
    if not directory:
        return None
    attempt_id = os.getenv("LONGHOUSE_DEPLOYMENT_ATTEMPT_ID", "").strip()
    if os.getenv("LONGHOUSE_DEPLOYMENT_PENDING", "").strip() != "1" or not attempt_id:
        raise RuntimeError(f"{HANDOFF_DIR_ENV} requires a pending deployment with LONGHOUSE_DEPLOYMENT_ATTEMPT_ID")
    from zerg.services.runtime_admission import runtime_admission

    _HANDOFF = CatalogHandoff(
        directory=Path(directory),
        attempt_id=attempt_id,
        runtime_epoch=runtime_admission().runtime_epoch,
        cutoff=_parse_time(os.getenv("LONGHOUSE_CLAIM_CUTOFF")),
    )
    return _HANDOFF


def catalog_handoff_pending() -> CatalogHandoff | None:
    """The handoff while its catalog is not ready yet, else None."""
    handoff = catalog_handoff()
    if handoff is None or handoff.ready.is_set():
        return None
    return handoff


def reset_catalog_handoff_for_tests(handoff: CatalogHandoff | None = None) -> None:
    global _HANDOFF, _HANDOFF_LOADED
    _HANDOFF = handoff
    _HANDOFF_LOADED = handoff is not None
