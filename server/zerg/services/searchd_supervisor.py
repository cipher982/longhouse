"""Non-fatal supervision for the disposable searchd process."""

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
from zerg.config import get_settings_unchecked
from zerg.config import sqlite_file_path
from zerg.searchd.store import SCHEMA_GENERATION
from zerg.searchd.store import SCHEMA_VERSION

logger = logging.getLogger(__name__)


def searchd_paths() -> tuple[Path, Path]:
    catalog_path = sqlite_file_path(get_settings_unchecked().live_database_url)
    if catalog_path is None:
        raise RuntimeError("searchd requires a file-backed live SQLite database")
    database_path = catalog_path.parent / "search.db"
    socket_directory = catalog_path.parent / ".searchd"
    longest_socket = socket_directory / f".searchd.sock.tmp.{os.getpid()}"
    if len(os.fsencode(longest_socket)) >= 104:
        digest = hashlib.sha256(os.fsencode(catalog_path.expanduser().resolve())).hexdigest()[:16]
        socket_directory = Path("/tmp") / f"lhsd-{os.getuid()}-{digest}"
    socket_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(socket_directory, 0o700)
    return database_path, socket_directory / "searchd.sock"


class SearchdSupervisor:
    """Keep searchd available without making it part of hot readiness."""

    def __init__(self, *, database_path: Path, socket_path: Path) -> None:
        self.database_path = database_path
        self.socket_path = socket_path
        self.status_path = socket_path.with_name("searchd-status.json")
        self.client = CatalogClient(socket_path)
        self._task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._restart_count = 0

    async def start(self, *, readiness_timeout_seconds: float = 2.0) -> dict[str, Any] | None:
        """Start supervision and return readiness if it arrives within the soft deadline.

        A timeout is deliberately non-fatal: the supervisor remains alive and
        keeps restarting searchd while the Runtime Host serves hot paths.
        """

        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="searchd-supervisor")
        deadline = asyncio.get_running_loop().time() + readiness_timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                ping = await self.client.call(
                    "search.ping.v2",
                    timeout_seconds=max(0.01, min(0.1, remaining)),
                )
                if self._is_compatible(ping):
                    self._write_status("running", ping=ping, ownership=self.ownership)
                    return ping
            except Exception as exc:  # search readiness never blocks startup
                last_error = exc
            await asyncio.sleep(0.05)
        self._write_status(
            "degraded",
            ownership=self.ownership,
            error=f"{type(last_error).__name__}: {last_error}" if last_error else "readiness timeout",
            restart_count=self._restart_count,
        )
        return None

    @property
    def ownership(self) -> str:
        return "owned" if self._process is not None and self._process.returncode is None else "adopted"

    async def stop(self) -> None:
        self._stopping = True
        await self.client.close()
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
                ping = await self.client.call("search.ping.v2")
            except CatalogUnavailable:
                ping = None
            if ping is not None and self._is_compatible(ping):
                self._write_status("running", ping=ping, ownership=self.ownership)
                await asyncio.sleep(0.5)
                continue
            if ping is not None:
                self._write_status("waiting_for_compatible_owner", ping=ping, ownership="adopted")
                await self.client.close()
                await asyncio.sleep(0.1)
                continue

            try:
                process = await self._spawn_process()
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
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 2)

    async def _spawn_process(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zerg.searchd",
            "--database",
            str(self.database_path),
            "--socket",
            str(self.socket_path),
        )

    async def _monitor_owned_process(self, process: asyncio.subprocess.Process) -> int:
        while process.returncode is None and not self._stopping:
            try:
                ping = await self.client.call("search.ping.v2")
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
            and ping.get("schema_version") == SCHEMA_VERSION
            and ping.get("schema_generation") == SCHEMA_GENERATION
        )

    def _write_status(self, status: str, **details: Any) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "schema_generation": SCHEMA_GENERATION,
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


_supervisor: SearchdSupervisor | None = None


async def start_searchd_supervisor() -> dict[str, Any] | None:
    global _supervisor
    if _supervisor is None:
        database_path, socket_path = searchd_paths()
        _supervisor = SearchdSupervisor(database_path=database_path, socket_path=socket_path)
    return await _supervisor.start()


async def stop_searchd_supervisor() -> None:
    global _supervisor
    if _supervisor is not None:
        await _supervisor.stop()
    _supervisor = None


def get_searchd_client() -> CatalogClient | None:
    return _supervisor.client if _supervisor is not None else None
