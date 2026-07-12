"""Narrow Unix-socket daemon that exclusively owns catalog schema work."""

from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import time
from pathlib import Path

from zerg.catalogd.protocol import CatalogRpcError
from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import ProtocolError
from zerg.catalogd.protocol import read_frame
from zerg.catalogd.protocol import write_frame
from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import CatalogMeta
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.schema import read_catalog_meta


class CatalogDaemonError(RuntimeError):
    pass


class CatalogDaemon:
    def __init__(
        self,
        *,
        database_path: Path,
        socket_path: Path,
        schema_generation: str = CATALOG_SCHEMA_GENERATION,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.socket_path = socket_path.expanduser().resolve()
        self.lock_path = self.database_path.with_suffix(f"{self.database_path.suffix}.catalogd.lock")
        self._lock_handle = None
        self._engine = None
        self._server: asyncio.AbstractServer | None = None
        self._published_inode: tuple[int, int] | None = None
        self._meta: CatalogMeta | None = None
        self._schema_generation = schema_generation

    async def start(self) -> CatalogMeta:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_parent = self.socket_path.parent.lstat()
        if stat.S_ISLNK(socket_parent.st_mode) or not stat.S_ISDIR(socket_parent.st_mode):
            raise CatalogDaemonError("catalog socket parent must be a directory, not a symlink")
        if socket_parent.st_uid != os.getuid():
            raise CatalogDaemonError("catalog socket parent is not owned by the runtime user")
        if stat.S_IMODE(socket_parent.st_mode) & 0o077:
            raise CatalogDaemonError("catalog socket parent must not be group/world accessible")
        self._acquire_lock()
        try:
            self._engine = create_catalog_engine(self.database_path)
            self._meta = initialize_catalog_schema(self._engine)
            if os.getenv("LONGHOUSE_CATALOGD_TEST_EXIT_AFTER_SCHEMA") == "1":
                os._exit(93)
            self._prepare_final_socket_path()
            temporary_socket = self.socket_path.with_name(f".{self.socket_path.name}.tmp.{os.getpid()}")
            if len(os.fsencode(temporary_socket)) >= 104:
                raise CatalogDaemonError("catalog socket path exceeds the portable Unix limit")
            temporary_socket.unlink(missing_ok=True)
            self._server = await asyncio.start_unix_server(self._handle_connection, path=temporary_socket)
            os.chmod(temporary_socket, 0o600)
            os.replace(temporary_socket, self.socket_path)
            published = self.socket_path.stat()
            self._published_inode = (published.st_dev, published.st_ino)
            return self._meta
        except BaseException:
            await self.close()
            raise

    async def serve_forever(self) -> None:
        if self._server is None:
            raise CatalogDaemonError("catalogd is not started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._unlink_published_socket()
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def _acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise CatalogDaemonError("catalog lock is already held") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._lock_handle = handle

    def _prepare_final_socket_path(self) -> None:
        try:
            entry = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISSOCK(entry.st_mode):
            raise CatalogDaemonError("catalog socket path exists and is not a socket")
        if entry.st_uid != os.getuid():
            raise CatalogDaemonError("catalog socket is not owned by the runtime user")
        self.socket_path.unlink()

    def _unlink_published_socket(self) -> None:
        if self._published_inode is None:
            return
        try:
            current = self.socket_path.stat()
        except FileNotFoundError:
            self._published_inode = None
            return
        if (current.st_dev, current.st_ino) == self._published_inode and stat.S_ISSOCK(current.st_mode):
            self.socket_path.unlink()
        self._published_inode = None

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                message = await read_frame(reader)
                if not isinstance(message, CatalogRpcRequest):
                    raise ProtocolError("invalid_request", "catalogd accepts request frames only")
                response = self._dispatch(message)
                await write_frame(writer, response)
        except (EOFError, asyncio.IncompleteReadError, ConnectionError, ProtocolError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def _dispatch(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if time.monotonic_ns() > int(request.deadline_mono_ns):
            return self._error(request, "deadline_exceeded", "request deadline exceeded", retryable=True)
        if request.params:
            return self._error(request, "invalid_request", "initial catalog methods accept empty params")
        if self._engine is None or self._meta is None:
            return self._error(request, "catalog_unavailable", "catalog is not ready", retryable=True)
        metadata = read_catalog_meta(self._engine)
        if request.method == "ping.v2":
            return CatalogRpcResponse(
                id=request.id,
                result={
                    "catalog_id": str(metadata.catalog_id),
                    "schema_generation": self._schema_generation,
                    "schema_version": metadata.schema_version,
                    "commit_seq": str(metadata.commit_seq),
                    "pid": os.getpid(),
                    "ready": True,
                },
            )
        if request.method == "schema.v2":
            return CatalogRpcResponse(
                id=request.id,
                result={
                    "catalog_id": str(metadata.catalog_id),
                    "schema_generation": self._schema_generation,
                    "schema_version": metadata.schema_version,
                    "minimum_reader_schema_version": CATALOG_SCHEMA_VERSION,
                    "maximum_reader_schema_version": CATALOG_SCHEMA_VERSION,
                    "commit_seq": str(metadata.commit_seq),
                },
            )
        return self._error(request, "unknown_method", f"unknown catalog method: {request.method}")

    @staticmethod
    def _error(
        request: CatalogRpcRequest,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> CatalogRpcResponse:
        return CatalogRpcResponse(
            id=request.id,
            error=CatalogRpcError(
                code=code,
                message=message,
                retryable=retryable,
                retry_after_ms=0 if retryable else None,
                details={},
            ),
        )


def socket_path_is_live(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)
