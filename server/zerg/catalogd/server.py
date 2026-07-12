"""Narrow Unix-socket daemon that exclusively owns catalog schema work."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import stat
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
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
from zerg.catalogd.store import CatalogStore

logger = logging.getLogger(__name__)


class CatalogDaemonError(RuntimeError):
    pass


class CatalogDaemon:
    def __init__(
        self,
        *,
        database_path: Path,
        socket_path: Path,
        schema_generation: str = CATALOG_SCHEMA_GENERATION,
        checkpoint_interval_seconds: float = 30.0,
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
        self._checkpoint_interval_seconds = checkpoint_interval_seconds
        self._executor: ThreadPoolExecutor | None = None
        self._store: CatalogStore | None = None
        self._checkpoint_task: asyncio.Task | None = None

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
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catalogd-sqlite")
            self._store = CatalogStore(self._engine)
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
            if self._checkpoint_interval_seconds > 0:
                self._checkpoint_task = asyncio.create_task(
                    self._checkpoint_loop(),
                    name="catalogd-checkpoint",
                )
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
        if self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._checkpoint_task
            self._checkpoint_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._unlink_published_socket()
        self._store = None
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
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
                try:
                    response = await self._dispatch(message)
                except Exception:
                    logger.exception("catalogd operation failed method=%s", message.method)
                    response = self._error(
                        message,
                        "internal",
                        "catalog operation failed",
                        # A mutation may have committed before its response was
                        # lost. Callers must reconcile/replay idempotently, not
                        # blindly retry an unknown operation.
                        retryable=False,
                    )
                await write_frame(writer, response)
        except (EOFError, asyncio.IncompleteReadError, ConnectionError, ProtocolError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _dispatch(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if time.monotonic_ns() > int(request.deadline_mono_ns):
            return self._error(request, "deadline_exceeded", "request deadline exceeded", retryable=True)
        if self._engine is None or self._meta is None or self._store is None:
            return self._error(request, "catalog_unavailable", "catalog is not ready", retryable=True)
        if request.method == "auth.device.validate.v2":
            return await self._authenticate_device(request)
        if request.method == "auth.device.create.v2":
            return await self._create_device(request)
        if request.method == "auth.device.list.v2":
            return await self._list_devices(request)
        if request.method == "auth.device.revoke.v2":
            return await self._revoke_device(request)
        if request.params:
            return self._error(request, "invalid_request", "catalog metadata methods accept empty params")
        metadata = await self._run_store(read_catalog_meta, self._engine)
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

    async def _authenticate_device(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"token_hash"}:
            return self._error(
                request,
                "invalid_request",
                "auth.device.validate.v2 requires token_hash",
            )
        token_hash = request.params["token_hash"]
        if not isinstance(token_hash, str) or len(token_hash) != 64 or any(character not in "0123456789abcdef" for character in token_hash):
            return self._error(request, "invalid_request", "token_hash must be 64 lowercase hexadecimal characters")
        assert self._store is not None
        result = await self._run_store(
            self._store.authenticate_device,
            token_hash=token_hash,
        )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _revoke_device(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"owner_id", "token_id"}:
            return self._error(
                request,
                "invalid_request",
                "auth.device.revoke.v2 requires owner_id and token_id",
            )
        owner_id = request.params["owner_id"]
        token_id = request.params["token_id"]
        if type(owner_id) is not int or owner_id <= 0:
            return self._error(request, "invalid_request", "owner_id must be a positive integer")
        if not isinstance(token_id, str) or not token_id or len(token_id) > 255:
            return self._error(request, "invalid_request", "token_id must be a non-empty string")
        assert self._store is not None
        result = await self._run_store(
            self._store.revoke_device,
            owner_id=owner_id,
            token_id=token_id,
        )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _create_device(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        required = {"owner_id", "token_id", "device_id", "token_hash"}
        if set(request.params) != required:
            return self._error(
                request,
                "invalid_request",
                "auth.device.create.v2 requires owner_id, token_id, device_id, and token_hash",
            )
        owner_id = request.params["owner_id"]
        token_id = request.params["token_id"]
        device_id = request.params["device_id"]
        token_hash = request.params["token_hash"]
        if type(owner_id) is not int or owner_id <= 0:
            return self._error(request, "invalid_request", "owner_id must be a positive integer")
        try:
            parsed_token_id = uuid.UUID(token_id) if isinstance(token_id, str) else None
        except ValueError:
            parsed_token_id = None
        if parsed_token_id is None or str(parsed_token_id) != token_id:
            return self._error(request, "invalid_request", "token_id must be a canonical UUID")
        if not isinstance(device_id, str) or not device_id or len(device_id) > 255:
            return self._error(request, "invalid_request", "device_id must contain 1 to 255 characters")
        if not isinstance(token_hash, str) or len(token_hash) != 64 or any(character not in "0123456789abcdef" for character in token_hash):
            return self._error(request, "invalid_request", "token_hash must be 64 lowercase hexadecimal characters")
        assert self._store is not None
        result = await self._run_store(
            self._store.create_device,
            owner_id=owner_id,
            token_id=token_id,
            device_id=device_id,
            token_hash=token_hash,
        )
        if result.get("exact_replay") is False and result.get("token_id") == token_id and result.get("created") is False:
            return self._error(request, "conflict", "token_id already exists with different attributes")
        if result.get("limit_exceeded") is True:
            return self._error(request, "resource_exhausted", "device token limit reached")
        return CatalogRpcResponse(id=request.id, result=result)

    async def _list_devices(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"owner_id", "include_revoked"}:
            return self._error(
                request,
                "invalid_request",
                "auth.device.list.v2 requires owner_id and include_revoked",
            )
        owner_id = request.params["owner_id"]
        include_revoked = request.params["include_revoked"]
        if type(owner_id) is not int or owner_id <= 0:
            return self._error(request, "invalid_request", "owner_id must be a positive integer")
        if type(include_revoked) is not bool:
            return self._error(request, "invalid_request", "include_revoked must be a boolean")
        assert self._store is not None
        result = await self._run_store(
            self._store.list_devices,
            owner_id=owner_id,
            include_revoked=include_revoked,
        )
        if result.get("limit_exceeded") is True:
            return self._error(request, "resource_exhausted", "device token list exceeds the catalog bound")
        return CatalogRpcResponse(id=request.id, result=result)

    async def _run_store(self, operation, *args, **kwargs):
        if self._executor is None:
            raise CatalogDaemonError("catalog executor is not ready")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: operation(*args, **kwargs))

    async def _checkpoint_loop(self) -> None:
        assert self._store is not None
        while True:
            await asyncio.sleep(self._checkpoint_interval_seconds)
            try:
                await self._run_store(self._store.checkpoint_passive)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("catalogd passive checkpoint failed")

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
