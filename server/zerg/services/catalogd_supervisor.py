"""Rollout-safe supervision for the catalogd process."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.config import get_settings_unchecked
from zerg.config import sqlite_file_path

logger = logging.getLogger(__name__)


def catalogd_paths() -> tuple[Path, Path]:
    database_path = sqlite_file_path(get_settings_unchecked().live_database_url)
    if database_path is None:
        raise RuntimeError("catalogd requires a file-backed live SQLite database")
    socket_directory = database_path.parent / ".catalogd"
    longest_socket = socket_directory / f".catalogd.sock.tmp.{os.getpid()}"
    if len(os.fsencode(longest_socket)) >= 104:
        digest = hashlib.sha256(os.fsencode(database_path.expanduser().resolve())).hexdigest()[:16]
        # `/tmp` remains short after macOS resolves it to `/private/tmp`; the
        # platform temp dir under `/private/var/folders/...` often does not.
        socket_directory = Path("/tmp") / f"lhcd-{os.getuid()}-{digest}"
    socket_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(socket_directory, 0o700)
    return database_path, socket_directory / "catalogd.sock"


class CatalogdSupervisor:
    def __init__(self, *, database_path: Path, socket_path: Path) -> None:
        self.database_path = database_path
        self.socket_path = socket_path
        self.status_path = socket_path.with_name("catalogd-status.json")
        self.client = CatalogClient(socket_path)
        self.projector_client = CatalogClient(socket_path)
        self._task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._restart_count = 0
        self._last_logged_status: tuple[object, ...] | None = None

    async def start(self, *, readiness_timeout_seconds: float = 15.0) -> dict[str, Any]:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="catalogd-supervisor")
        deadline = asyncio.get_running_loop().time() + readiness_timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                ping = await self.client.call("ping.v2")
                if self._is_compatible(ping):
                    self._write_status("running", ping=ping, ownership=self.ownership)
                    return ping
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.05)
        await self.stop()
        raise RuntimeError(f"catalogd did not become ready: {last_error}")

    @property
    def ownership(self) -> str:
        return "owned" if self._process is not None and self._process.returncode is None else "adopted"

    async def stop(self) -> None:
        self._stopping = True
        # Catalog calls own short-lived connections. Keep the compatibility
        # close boundary before SIGTERM so future client resources drain first.
        await self.client.close()
        await self.projector_client.close()
        await self._terminate_owned_process()
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None
        self._write_status("stopped", ownership="none")

    async def _run(self) -> None:
        backoff = 0.1
        while not self._stopping:
            try:
                ping = await self.client.call("ping.v2")
            except CatalogUnavailable:
                ping = None
            if ping is not None and self._is_compatible(ping):
                self._write_status("running", ping=ping, ownership=self.ownership)
                await asyncio.sleep(0.5)
                continue
            if ping is not None:
                # A blue-green peer still owns the shared socket with an older
                # table shape. Never claim readiness or race its schema lock.
                self._write_status("waiting_for_compatible_owner", ping=ping, ownership="adopted")
                await self.client.close()
                await asyncio.sleep(0.1)
                continue

            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "zerg.catalogd",
                    "--database",
                    str(self.database_path),
                    "--socket",
                    str(self.socket_path),
                )
                self._process = process
                self._write_status("starting", ownership="owned", pid=process.pid)
                returncode = await self._monitor_owned_process(process)
                if self._stopping:
                    return
                self._restart_count += 1
                self._write_status(
                    "degraded",
                    ownership="none",
                    last_exit_code=returncode,
                    restart_count=self._restart_count,
                )
            except asyncio.CancelledError:
                await self._terminate_owned_process()
                raise
            except Exception as exc:
                self._restart_count += 1
                self._write_status(
                    "degraded",
                    ownership="none",
                    error=f"{type(exc).__name__}: {exc}",
                    restart_count=self._restart_count,
                )
            finally:
                if self._process is not None and self._process.returncode is not None:
                    self._process = None
                await self.client.close()
                await self.projector_client.close()
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 2)

    async def _monitor_owned_process(self, process: asyncio.subprocess.Process) -> int:
        """Publish live owned-daemon state while retaining process supervision."""

        while process.returncode is None and not self._stopping:
            try:
                ping = await self.client.call("ping.v2")
                if self._is_compatible(ping):
                    self._write_status(
                        "running",
                        ping=ping,
                        ownership="owned",
                        restart_count=self._restart_count,
                    )
            except CatalogUnavailable as exc:
                self._write_status(
                    "degraded",
                    log_transition=False,
                    ownership="owned",
                    pid=process.pid,
                    error=f"{type(exc).__name__}: {exc}",
                    restart_count=self._restart_count,
                )
            try:
                return await asyncio.wait_for(process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
        return await process.wait()

    async def _terminate_owned_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        self._process = None

    @staticmethod
    def _is_compatible(ping: dict[str, Any]) -> bool:
        return (
            ping.get("ready") is True
            and ping.get("schema_version") == CATALOG_SCHEMA_VERSION
            and ping.get("schema_generation") == CATALOG_SCHEMA_GENERATION
        )

    def _write_status(self, status: str, *, log_transition: bool = True, **details: Any) -> None:
        if log_transition:
            self._log_status_transition(status, details)
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "schema_generation": CATALOG_SCHEMA_GENERATION,
            "status": status,
            "observed_at_unix": time.time(),
            "supervisor_pid": os.getpid(),
            **details,
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.status_path.name}.", dir=self.status_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
            os.replace(temporary, self.status_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _log_status_transition(self, status: str, details: dict[str, Any]) -> None:
        restart_count = details.get("restart_count", self._restart_count)
        signature = (
            status,
            details.get("ownership"),
            restart_count,
            details.get("last_exit_code"),
            details.get("error"),
        )
        if signature == self._last_logged_status:
            return
        self._last_logged_status = signature
        level = logging.WARNING if status in {"degraded", "waiting_for_compatible_owner"} else logging.INFO
        context = {
            "tag": "CATALOGD",
            "event": "supervisor_state_changed",
            "status": status,
            "ownership": details.get("ownership", "unknown"),
            "restart_count": restart_count,
        }
        for key in ("pid", "last_exit_code", "error"):
            if key in details:
                context[key] = details[key]
        logger.log(level, "catalogd supervisor state changed", extra=context)


_supervisor: CatalogdSupervisor | None = None


async def start_catalogd_supervisor() -> dict[str, Any]:
    global _supervisor
    if _supervisor is None:
        database_path, socket_path = catalogd_paths()
        _supervisor = CatalogdSupervisor(database_path=database_path, socket_path=socket_path)
    return await _supervisor.start()


async def stop_catalogd_supervisor() -> None:
    global _supervisor
    if _supervisor is not None:
        await _supervisor.stop()
    _supervisor = None


def get_catalogd_client() -> CatalogClient | None:
    return _supervisor.client if _supervisor is not None else None


def get_catalogd_projector_client() -> CatalogClient | None:
    return _supervisor.projector_client if _supervisor is not None else None
