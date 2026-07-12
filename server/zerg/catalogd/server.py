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
from datetime import UTC
from datetime import datetime
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
        if request.method == "auth.device.resolve.v2":
            return await self._resolve_device(request)
        if request.method == "auth.device.create.v2":
            return await self._create_device(request)
        if request.method == "auth.device.list.v2":
            return await self._list_devices(request)
        if request.method == "auth.device.revoke.v2":
            return await self._revoke_device(request)
        if request.method == "auth.user.get.v2":
            return await self._get_user(request)
        if request.method == "auth.owner.get.v2":
            return await self._get_active_owner(request)
        if request.method == "auth.user.resolve_cp.v2":
            return await self._resolve_cp_user(request)
        if request.method == "auth.user.resolve_local.v2":
            return await self._resolve_local_user(request)
        if request.method == "auth.user.update.v2":
            return await self._update_user(request)
        if request.method == "auth.refresh.create.v2":
            return await self._create_refresh(request)
        if request.method == "auth.refresh.rotate.v2":
            return await self._rotate_refresh(request)
        if request.method == "auth.refresh.revoke_family.v2":
            return await self._revoke_refresh_family(request)
        if request.method == "machine.heartbeat.apply.v2":
            return await self._apply_machine_heartbeat(request)
        if request.method == "session.runtime.apply.v2":
            return await self._apply_session_runtime(request)
        if request.method == "control.command_result.apply.v2":
            return await self._apply_control_command_result(request)
        if request.method == "control.command.prepare.v2":
            return await self._prepare_control_command(request)
        if request.method == "control.operation.finish.v2":
            return await self._finish_control_operation(request)
        if request.method == "session.launch.idempotency.v2":
            return await self._read_launch_idempotency(request)
        if request.method == "session.launch.intent.create.v2":
            return await self._create_launch_intent(request)
        if request.method == "session.launch.outcome.apply.v2":
            return await self._apply_launch_outcome(request)
        if request.method == "session.input.queued.list.v2":
            return await self._list_queued_input_sessions(request)
        if request.method == "session.input.claim.v2":
            return await self._claim_queued_input(request)
        if request.method == "session.input.finish.v2":
            return await self._finish_queued_input(request)
        if request.method == "session.timeline.list.v2":
            return await self._list_session_timeline(request)
        if request.method == "session.read.v2":
            return await self._read_session(request)
        if request.method == "session.prefix.resolve.v2":
            return await self._resolve_session_prefix(request)
        if request.method == "machine.enrollment.list.v2":
            return await self._list_machine_enrollments(request)
        if request.method == "machine.workspace.list.v2":
            return await self._list_machine_workspaces(request)
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

    async def _get_user(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"user_id", "touch_last_login"}:
            return self._error(request, "invalid_request", "auth.user.get.v2 requires user_id and touch_last_login")
        user_id = request.params["user_id"]
        touch = request.params["touch_last_login"]
        if type(user_id) is not int or user_id <= 0:
            return self._error(request, "invalid_request", "user_id must be a positive integer")
        if type(touch) is not bool:
            return self._error(request, "invalid_request", "touch_last_login must be a boolean")
        assert self._store is not None
        return CatalogRpcResponse(
            id=request.id, result=await self._run_store(self._store.get_user, user_id=user_id, touch_last_login=touch)
        )

    async def _get_active_owner(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if request.params:
            return self._error(request, "invalid_request", "auth.owner.get.v2 accepts no parameters")
        assert self._store is not None
        return CatalogRpcResponse(
            id=request.id,
            result=await self._run_store(self._store.get_active_owner),
        )

    async def _resolve_device(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {"token_hash", "touch_last_used", "touch_interval_seconds"}
        if set(request.params) != expected:
            return self._error(
                request,
                "invalid_request",
                "auth.device.resolve.v2 requires token_hash, touch_last_used, and touch_interval_seconds",
            )
        token_hash = request.params["token_hash"]
        touch = request.params["touch_last_used"]
        interval = request.params["touch_interval_seconds"]
        if not _is_hash(token_hash):
            return self._error(request, "invalid_request", "token_hash must be 64 lowercase hexadecimal characters")
        if type(touch) is not bool:
            return self._error(request, "invalid_request", "touch_last_used must be a boolean")
        if type(interval) is not int or not 0 <= interval <= 86_400:
            return self._error(request, "invalid_request", "touch_interval_seconds must be an integer from 0 through 86400")
        assert self._store is not None
        result = await self._run_store(
            self._store.resolve_device,
            token_hash=token_hash,
            touch_last_used=touch,
            touch_interval_seconds=interval,
        )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _resolve_cp_user(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {"cp_user_id", "email", "email_verified", "display_name", "avatar_url"}
        if set(request.params) != expected:
            return self._error(
                request,
                "invalid_request",
                "auth.user.resolve_cp.v2 requires cp_user_id, email, email_verified, display_name, and avatar_url",
            )
        params = request.params
        if type(params["cp_user_id"]) is not int or params["cp_user_id"] <= 0:
            return self._error(request, "invalid_request", "cp_user_id must be a positive integer")
        if not _is_string(params["email"], maximum=320):
            return self._error(request, "invalid_request", "email must contain 1 to 320 characters")
        if type(params["email_verified"]) is not bool:
            return self._error(request, "invalid_request", "email_verified must be a boolean")
        for field in ("display_name", "avatar_url"):
            if params[field] is not None and not isinstance(params[field], str):
                return self._error(request, "invalid_request", f"{field} must be a string or null")
        assert self._store is not None
        result = await self._run_store(self._store.resolve_cp_user, **params)
        if conflict := result.get("conflict"):
            return self._error(request, "conflict", "control-plane identity conflicts with catalog state", details={"reason": conflict})
        return CatalogRpcResponse(id=request.id, result=result)

    async def _resolve_local_user(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {
            "email",
            "provider",
            "provider_user_id",
            "role",
            "adopt_existing",
            "require_email_match",
            "max_users",
            "promote_role",
        }
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "auth.user.resolve_local.v2 has invalid parameters")
        params = request.params
        if not _is_string(params["email"], maximum=320) or not _is_string(params["provider"], maximum=64):
            return self._error(request, "invalid_request", "email and provider must be non-empty bounded strings")
        if params["provider_user_id"] is not None and not _is_string(params["provider_user_id"], maximum=255):
            return self._error(request, "invalid_request", "provider_user_id must be a non-empty string or null")
        if params["role"] not in {"USER", "ADMIN"}:
            return self._error(request, "invalid_request", "role must be USER or ADMIN")
        for field in ("adopt_existing", "require_email_match", "promote_role"):
            if type(params[field]) is not bool:
                return self._error(request, "invalid_request", f"{field} must be a boolean")
        if params["max_users"] is not None and (type(params["max_users"]) is not int or params["max_users"] <= 0):
            return self._error(request, "invalid_request", "max_users must be a positive integer or null")
        assert self._store is not None
        result = await self._run_store(self._store.resolve_local_user, **params)
        if conflict := result.get("conflict"):
            return self._error(request, "conflict", "local identity conflicts with catalog state", details={"reason": conflict})
        return CatalogRpcResponse(id=request.id, result=result)

    async def _update_user(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {"user_id", "display_name", "avatar_url", "prefs", "update_mask"}
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "auth.user.update.v2 has invalid parameters")
        params = request.params
        if type(params["user_id"]) is not int or params["user_id"] <= 0:
            return self._error(request, "invalid_request", "user_id must be a positive integer")
        for field in ("display_name", "avatar_url"):
            if params[field] is not None and not isinstance(params[field], str):
                return self._error(request, "invalid_request", f"{field} must be a string or null")
        if params["prefs"] is not None and not isinstance(params["prefs"], dict):
            return self._error(request, "invalid_request", "prefs must be an object or null")
        mask = params["update_mask"]
        if (
            not isinstance(mask, list)
            or any(not isinstance(item, str) for item in mask)
            or len(mask) != len(set(mask))
            or not set(mask) <= {"display_name", "avatar_url", "prefs"}
        ):
            return self._error(request, "invalid_request", "update_mask must contain unique profile field names")
        assert self._store is not None
        result = await self._run_store(self._store.update_user, **params)
        return CatalogRpcResponse(id=request.id, result=result)

    async def _create_refresh(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {
            "user_id",
            "token_hash",
            "family_id",
            "parent_id",
            "created_at",
            "absolute_expires_at",
            "idle_expires_at",
        }
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "auth.refresh.create.v2 has invalid parameters")
        params = dict(request.params)
        if type(params["user_id"]) is not int or params["user_id"] <= 0 or not _is_hash(params["token_hash"]):
            return self._error(request, "invalid_request", "user_id or token_hash is invalid")
        if not _is_string(params["family_id"], maximum=64):
            return self._error(request, "invalid_request", "family_id must contain 1 to 64 characters")
        if params["parent_id"] is not None and (type(params["parent_id"]) is not int or params["parent_id"] <= 0):
            return self._error(request, "invalid_request", "parent_id must be a positive integer or null")
        try:
            for field in ("created_at", "absolute_expires_at", "idle_expires_at"):
                params[field] = _parse_datetime(params[field], field)
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        if not params["created_at"] < params["idle_expires_at"] <= params["absolute_expires_at"]:
            return self._error(request, "invalid_request", "refresh expiry ordering is invalid")
        assert self._store is not None
        result = await self._run_store(self._store.create_refresh_session, **params)
        if not_found := result.get("not_found"):
            return self._error(
                request,
                "conflict",
                "refresh session owner does not exist",
                details={"reason": f"{not_found}_not_found"},
            )
        if result.get("created") is False and result.get("exact_replay") is False:
            return self._error(
                request,
                "conflict",
                "token_hash already exists with different attributes",
                details={"reason": "token_hash_collision"},
            )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _rotate_refresh(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {"token_hash", "next_token_hash", "now", "idle_expires_at", "reuse_grace_seconds"}
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "auth.refresh.rotate.v2 has invalid parameters")
        params = dict(request.params)
        if not _is_hash(params["token_hash"]) or not _is_hash(params["next_token_hash"]):
            return self._error(request, "invalid_request", "refresh token hashes must be lowercase hexadecimal SHA-256 values")
        if params["token_hash"] == params["next_token_hash"]:
            return self._error(request, "invalid_request", "next_token_hash must differ from token_hash")
        if type(params["reuse_grace_seconds"]) is not int or not 0 <= params["reuse_grace_seconds"] <= 300:
            return self._error(request, "invalid_request", "reuse_grace_seconds must be an integer from 0 through 300")
        try:
            params["now"] = _parse_datetime(params["now"], "now")
            params["idle_expires_at"] = _parse_datetime(params["idle_expires_at"], "idle_expires_at")
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        if params["idle_expires_at"] <= params["now"]:
            return self._error(request, "invalid_request", "idle_expires_at must be after now")
        assert self._store is not None
        result = await self._run_store(self._store.rotate_refresh_session, **params)
        if conflict := result.get("conflict"):
            return self._error(request, "conflict", "refresh rotation conflicts with catalog state", details={"reason": conflict})
        return CatalogRpcResponse(id=request.id, result=result)

    async def _revoke_refresh_family(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"token_hash", "now"} or not _is_hash(request.params.get("token_hash")):
            return self._error(request, "invalid_request", "auth.refresh.revoke_family.v2 requires token_hash and now")
        try:
            now = _parse_datetime(request.params["now"], "now")
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        assert self._store is not None
        result = await self._run_store(self._store.revoke_refresh_family, token_hash=request.params["token_hash"], now=now)
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

    async def _apply_machine_heartbeat(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {"heartbeat", "managed_leases", "managed_leases_present", "owner_id"}
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "machine.heartbeat.apply.v2 has invalid parameters")
        heartbeat = request.params["heartbeat"]
        leases = request.params["managed_leases"]
        snapshot_present = request.params["managed_leases_present"]
        owner_id = request.params["owner_id"]
        if not isinstance(heartbeat, dict):
            return self._error(request, "invalid_request", "heartbeat must be an object")
        if not isinstance(leases, list) or len(leases) > 512:
            return self._error(request, "invalid_request", "managed_leases must contain at most 512 rows")
        if type(snapshot_present) is not bool:
            return self._error(request, "invalid_request", "managed_leases_present must be a boolean")
        if owner_id is not None and (type(owner_id) is not int or owner_id <= 0):
            return self._error(request, "invalid_request", "owner_id must be a positive integer or null")
        try:
            parsed_heartbeat = _validate_heartbeat_stamp(heartbeat)
            parsed_leases = [_validate_managed_lease(row) for row in leases]
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        assert self._store is not None
        result = await self._run_store(
            self._store.apply_machine_heartbeat,
            heartbeat=parsed_heartbeat,
            managed_leases=parsed_leases,
            managed_leases_present=snapshot_present,
            owner_id=owner_id,
        )
        if result.get("idempotency_conflict") is True:
            return self._error(
                request,
                "conflict",
                "heartbeat idempotency identity was reused with different content",
                details={"reason": "idempotency_conflict"},
            )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _apply_session_runtime(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"events"}:
            return self._error(request, "invalid_request", "session.runtime.apply.v2 has invalid parameters")
        raw_events = request.params["events"]
        if not isinstance(raw_events, list) or not 1 <= len(raw_events) <= 128:
            return self._error(request, "invalid_request", "events must contain 1 through 128 rows")
        from pydantic import ValidationError

        from zerg.services.session_runtime import RuntimeEventIngest

        try:
            events = [RuntimeEventIngest.model_validate(item) for item in raw_events]
        except ValidationError as exc:
            return self._error(
                request,
                "invalid_request",
                "runtime event validation failed",
                details={"error_count": len(exc.errors())},
            )
        assert self._store is not None
        result = await self._run_store(self._store.apply_session_runtime, events=events)
        return CatalogRpcResponse(id=request.id, result=result)

    async def _apply_control_command_result(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"owner_id", "device_id", "message"}:
            return self._error(request, "invalid_request", "control.command_result.apply.v2 has invalid parameters")
        owner_id = request.params["owner_id"]
        device_id = request.params["device_id"]
        message = request.params["message"]
        if type(owner_id) is not int or owner_id < 0:
            return self._error(request, "invalid_request", "owner_id must be a non-negative integer")
        if not _is_string(device_id, maximum=255):
            return self._error(request, "invalid_request", "device_id must contain 1 to 255 characters")
        try:
            normalized_message = _validate_control_command_result(message)
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        assert self._store is not None
        result = await self._run_store(
            self._store.apply_control_command_result,
            owner_id=owner_id,
            device_id=device_id,
            message=normalized_message,
        )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _prepare_control_command(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {
            "operation_id",
            "owner_id",
            "session_id",
            "device_id",
            "provider",
            "command_type",
            "command_id",
            "capability",
            "request_payload",
            "timeout_secs",
        }
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "control.command.prepare.v2 has invalid parameters")
        params = dict(request.params)
        for field in ("operation_id", "session_id"):
            value = params[field]
            try:
                parsed = uuid.UUID(value) if isinstance(value, str) else None
            except ValueError:
                parsed = None
            if parsed is None or str(parsed) != value:
                return self._error(request, "invalid_request", f"{field} must be a canonical UUID")
        if type(params["owner_id"]) is not int or params["owner_id"] <= 0:
            return self._error(request, "invalid_request", "owner_id must be a positive integer")
        for field, maximum in (("device_id", 255), ("provider", 64), ("command_type", 64), ("command_id", 96)):
            if not _is_string(params[field], maximum=maximum):
                return self._error(request, "invalid_request", f"{field} must contain 1 to {maximum} characters")
        if params["capability"] not in {"send", "interrupt", "terminate"}:
            return self._error(request, "invalid_request", "capability is not recognized")
        if not isinstance(params["request_payload"], dict):
            return self._error(request, "invalid_request", "request_payload must be an object")
        if type(params["timeout_secs"]) is not int or not 1 <= params["timeout_secs"] <= 300:
            return self._error(request, "invalid_request", "timeout_secs must be an integer from 1 through 300")
        assert self._store is not None
        result = await self._run_store(self._store.prepare_control_command, **params)
        if result.get("reason") == "idempotency_conflict":
            return self._error(request, "conflict", "command_id was reused with different attributes")
        return CatalogRpcResponse(id=request.id, result=result)

    async def _finish_control_operation(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"operation_id", "status", "result", "error"}:
            return self._error(request, "invalid_request", "control.operation.finish.v2 has invalid parameters")
        operation_id = request.params["operation_id"]
        try:
            parsed = uuid.UUID(operation_id) if isinstance(operation_id, str) else None
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != operation_id:
            return self._error(request, "invalid_request", "operation_id must be a canonical UUID")
        status = request.params["status"]
        if status not in {"succeeded", "failed", "timed_out"}:
            return self._error(request, "invalid_request", "status is not terminal")
        result_payload = request.params["result"]
        error_payload = request.params["error"]
        if result_payload is not None and not isinstance(result_payload, dict):
            return self._error(request, "invalid_request", "result must be an object or null")
        if error_payload is not None and not isinstance(error_payload, dict):
            return self._error(request, "invalid_request", "error must be an object or null")
        assert self._store is not None
        result = await self._run_store(
            self._store.finish_control_operation,
            operation_id=operation_id,
            status=status,
            result=result_payload,
            error=error_payload,
        )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _read_launch_idempotency(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"owner_id", "device_id", "provider", "client_request_id"}:
            return self._error(request, "invalid_request", "session.launch.idempotency.v2 has invalid parameters")
        params = dict(request.params)
        if type(params["owner_id"]) is not int or params["owner_id"] <= 0:
            return self._error(request, "invalid_request", "owner_id must be a positive integer")
        for field, maximum in (("device_id", 255), ("provider", 64), ("client_request_id", 255)):
            if not _is_string(params[field], maximum=maximum):
                return self._error(request, "invalid_request", f"{field} must contain 1 to {maximum} characters")
        assert self._store is not None
        result = await self._run_store(self._store.read_launch_idempotency, **params)
        return CatalogRpcResponse(id=request.id, result=result)

    async def _create_launch_intent(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"launch"}:
            return self._error(request, "invalid_request", "session.launch.intent.create.v2 requires launch")
        try:
            launch = _validate_launch_rpc(request.params["launch"])
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        assert self._store is not None
        result = await self._run_store(self._store.create_launch_intent, launch=launch)
        if result.get("idempotency_conflict") is True:
            return self._error(request, "conflict", "launch command identity was reused with different attributes")
        return CatalogRpcResponse(id=request.id, result=result)

    async def _apply_launch_outcome(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"launch", "outcome"}:
            return self._error(request, "invalid_request", "session.launch.outcome.apply.v2 has invalid parameters")
        try:
            launch = _validate_launch_rpc(request.params["launch"])
            outcome = _validate_launch_outcome(request.params["outcome"])
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        assert self._store is not None
        result = await self._run_store(self._store.apply_launch_outcome, launch=launch, outcome=outcome)
        if result.get("found") is not True:
            return self._error(
                request,
                "conflict",
                "launch intent was not found",
                details={"reason": "launch_not_found"},
            )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _list_queued_input_sessions(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"limit"}:
            return self._error(request, "invalid_request", "session.input.queued.list.v2 requires limit")
        limit = request.params["limit"]
        if type(limit) is not int or not 1 <= limit <= 100:
            return self._error(request, "invalid_request", "limit must be an integer from 1 through 100")
        assert self._store is not None
        result = await self._run_store(self._store.list_queued_input_sessions, limit=limit)
        return CatalogRpcResponse(id=request.id, result=result)

    async def _claim_queued_input(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"session_id", "delivery_request_id"}:
            return self._error(request, "invalid_request", "session.input.claim.v2 has invalid parameters")
        session_id = request.params["session_id"]
        try:
            parsed = uuid.UUID(session_id) if isinstance(session_id, str) else None
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != session_id:
            return self._error(request, "invalid_request", "session_id must be a canonical UUID")
        delivery_request_id = request.params["delivery_request_id"]
        if not _is_string(delivery_request_id, maximum=64):
            return self._error(request, "invalid_request", "delivery_request_id must contain 1 to 64 characters")
        assert self._store is not None
        result = await self._run_store(
            self._store.claim_queued_input,
            session_id=session_id,
            delivery_request_id=delivery_request_id,
        )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _finish_queued_input(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"receipt_id", "delivery_request_id", "status", "error"}:
            return self._error(request, "invalid_request", "session.input.finish.v2 has invalid parameters")
        receipt_id = request.params["receipt_id"]
        try:
            parsed = uuid.UUID(receipt_id) if isinstance(receipt_id, str) else None
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != receipt_id:
            return self._error(request, "invalid_request", "receipt_id must be a canonical UUID")
        delivery_request_id = request.params["delivery_request_id"]
        if not _is_string(delivery_request_id, maximum=64):
            return self._error(request, "invalid_request", "delivery_request_id must contain 1 to 64 characters")
        status = request.params["status"]
        if status not in {"delivered", "failed"}:
            return self._error(request, "invalid_request", "status must be delivered or failed")
        error = request.params["error"]
        if error is not None and (not isinstance(error, str) or not error or len(error) > 500):
            return self._error(request, "invalid_request", "error must be null or contain 1 to 500 characters")
        assert self._store is not None
        result = await self._run_store(
            self._store.finish_queued_input,
            receipt_id=receipt_id,
            delivery_request_id=delivery_request_id,
            status=status,
            error=error,
        )
        return CatalogRpcResponse(id=request.id, result=result)

    async def _list_session_timeline(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {
            "project",
            "provider",
            "environment",
            "include_test",
            "hide_autonomous",
            "include_automation",
            "device_id",
            "days_back",
            "limit",
            "offset",
        }
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "session.timeline.list.v2 has invalid parameters")
        params = dict(request.params)
        for field, maximum in (("project", 255), ("provider", 64), ("environment", 32), ("device_id", 255)):
            value = params[field]
            if value is not None and (not isinstance(value, str) or not value or len(value) > maximum):
                return self._error(request, "invalid_request", f"{field} must be null or contain 1 to {maximum} characters")
        for field in ("include_test", "hide_autonomous", "include_automation"):
            if type(params[field]) is not bool:
                return self._error(request, "invalid_request", f"{field} must be a boolean")
        if type(params["days_back"]) is not int or not 1 <= params["days_back"] <= 90:
            return self._error(request, "invalid_request", "days_back must be an integer from 1 through 90")
        if type(params["limit"]) is not int or not 1 <= params["limit"] <= 100:
            return self._error(request, "invalid_request", "limit must be an integer from 1 through 100")
        if type(params["offset"]) is not int or not 0 <= params["offset"] <= 1_000_000:
            return self._error(request, "invalid_request", "offset must be an integer from 0 through 1000000")
        assert self._store is not None
        result = await self._run_store(self._store.list_session_timeline, **params)
        return CatalogRpcResponse(id=request.id, result=result)

    async def _read_session(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"session_id"}:
            return self._error(request, "invalid_request", "session.read.v2 requires session_id")
        session_id = request.params["session_id"]
        try:
            parsed = uuid.UUID(session_id) if isinstance(session_id, str) else None
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != session_id:
            return self._error(request, "invalid_request", "session_id must be a canonical UUID")
        assert self._store is not None
        result = await self._run_store(self._store.read_session, session_id=session_id)
        return CatalogRpcResponse(id=request.id, result=result)

    async def _resolve_session_prefix(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"prefix"}:
            return self._error(request, "invalid_request", "session.prefix.resolve.v2 requires prefix")
        prefix = request.params["prefix"]
        if (
            not isinstance(prefix, str)
            or not 1 <= len(prefix) <= 36
            or prefix != prefix.strip().lower()
            or any(character not in "0123456789abcdef-" for character in prefix)
        ):
            return self._error(request, "invalid_request", "prefix must be 1 to 36 lowercase UUID characters")
        assert self._store is not None
        result = await self._run_store(self._store.resolve_session_prefix, prefix=prefix)
        return CatalogRpcResponse(id=request.id, result=result)

    async def _list_machine_enrollments(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        if set(request.params) != {"owner_id"}:
            return self._error(request, "invalid_request", "machine.enrollment.list.v2 requires owner_id")
        owner_id = request.params["owner_id"]
        if type(owner_id) is not int or owner_id <= 0:
            return self._error(request, "invalid_request", "owner_id must be a positive integer")
        assert self._store is not None
        result = await self._run_store(self._store.list_machine_enrollments, owner_id=owner_id)
        if result.get("limit_exceeded") is True:
            return self._error(request, "resource_exhausted", "machine enrollment list exceeds the catalog bound")
        return CatalogRpcResponse(id=request.id, result=result)

    async def _list_machine_workspaces(self, request: CatalogRpcRequest) -> CatalogRpcResponse:
        expected = {"owner_id", "device_id", "limit", "days_back"}
        if set(request.params) != expected:
            return self._error(request, "invalid_request", "machine.workspace.list.v2 has invalid parameters")
        params = dict(request.params)
        if type(params["owner_id"]) is not int or params["owner_id"] <= 0:
            return self._error(request, "invalid_request", "owner_id must be a positive integer")
        if not isinstance(params["device_id"], str) or not 1 <= len(params["device_id"]) <= 255:
            return self._error(request, "invalid_request", "device_id must contain 1 to 255 characters")
        if type(params["limit"]) is not int or not 1 <= params["limit"] <= 50:
            return self._error(request, "invalid_request", "limit must be an integer from 1 through 50")
        if type(params["days_back"]) is not int or not 1 <= params["days_back"] <= 180:
            return self._error(request, "invalid_request", "days_back must be an integer from 1 through 180")
        assert self._store is not None
        result = await self._run_store(self._store.list_machine_workspaces, **params)
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
        details: dict | None = None,
    ) -> CatalogRpcResponse:
        return CatalogRpcResponse(
            id=request.id,
            error=CatalogRpcError(
                code=code,
                message=message,
                retryable=retryable,
                retry_after_ms=0 if retryable else None,
                details=details or {},
            ),
        )


_HEARTBEAT_FIELDS = {
    "device_id",
    "received_at",
    "version",
    "last_ship_at",
    "last_ship_attempt_at",
    "last_ship_result",
    "last_ship_latency_ms",
    "last_ship_http_status",
    "spool_pending",
    "spool_dead",
    "parse_errors_1h",
    "consecutive_failures",
    "ship_attempts_1h",
    "ship_successes_1h",
    "ship_rate_limited_1h",
    "ship_server_errors_1h",
    "ship_payload_rejections_1h",
    "ship_payload_too_large_1h",
    "ship_retryable_client_errors_1h",
    "ship_connect_errors_1h",
    "ship_latency_p50_ms_1h",
    "ship_latency_p95_ms_1h",
    "disk_free_bytes",
    "is_offline",
    "raw_json",
    "sessions_digest",
    "sessions_sequence",
}
_HEARTBEAT_REQUIRED_INTEGER_FIELDS = {
    "spool_pending",
    "spool_dead",
    "parse_errors_1h",
    "consecutive_failures",
    "ship_attempts_1h",
    "ship_successes_1h",
    "ship_rate_limited_1h",
    "ship_server_errors_1h",
    "ship_payload_rejections_1h",
    "ship_payload_too_large_1h",
    "ship_retryable_client_errors_1h",
    "ship_connect_errors_1h",
    "disk_free_bytes",
}
_HEARTBEAT_OPTIONAL_INTEGER_FIELDS = {
    "last_ship_latency_ms",
    "last_ship_http_status",
    "ship_latency_p50_ms_1h",
    "ship_latency_p95_ms_1h",
    "sessions_sequence",
}
_MANAGED_LEASE_FIELDS = {
    "session_id",
    "provider",
    "machine_id",
    "sequence",
    "state",
    "phase",
    "tool_name",
    "bridge_status",
    "thread_subscription_status",
    "observed_at",
    "lease_ttl_ms",
}
_LAUNCH_FIELDS = {
    "session_id",
    "primary_thread_id",
    "run_id",
    "owner_id",
    "device_id",
    "machine_id",
    "provider",
    "cwd",
    "git_repo",
    "git_branch",
    "project",
    "display_name",
    "initial_prompt",
    "execution_lifetime",
    "client_request_id",
    "command_id",
    "started_at",
    "expires_at",
    "launch_actor",
    "launch_surface",
}


def _validate_launch_rpc(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _LAUNCH_FIELDS:
        raise ValueError("launch has invalid fields")
    result = dict(value)
    for field in ("session_id", "primary_thread_id"):
        raw = result[field]
        try:
            parsed = uuid.UUID(raw) if isinstance(raw, str) else None
        except ValueError:
            parsed = None
        if parsed is None or str(parsed) != raw:
            raise ValueError(f"launch.{field} must be a canonical UUID")
    run_id = result["run_id"]
    if run_id is not None:
        try:
            parsed_run = uuid.UUID(run_id) if isinstance(run_id, str) else None
        except ValueError:
            parsed_run = None
        if parsed_run is None or str(parsed_run) != run_id:
            raise ValueError("launch.run_id must be a canonical UUID or null")
    if type(result["owner_id"]) is not int or result["owner_id"] <= 0:
        raise ValueError("launch.owner_id must be a positive integer")
    for field, maximum in (
        ("device_id", 255),
        ("machine_id", 255),
        ("provider", 64),
        ("cwd", 4096),
        ("project", 255),
        ("display_name", 255),
        ("command_id", 96),
    ):
        if not _is_string(result[field], maximum=maximum):
            raise ValueError(f"launch.{field} must contain 1 to {maximum} characters")
    for field, maximum in (
        ("git_repo", 500),
        ("git_branch", 255),
        ("client_request_id", 255),
        ("launch_actor", 32),
        ("launch_surface", 32),
    ):
        raw = result[field]
        if raw is not None and (not isinstance(raw, str) or not raw or len(raw) > maximum):
            raise ValueError(f"launch.{field} must be null or contain 1 to {maximum} characters")
    prompt = result["initial_prompt"]
    if prompt is not None and (not isinstance(prompt, str) or len(prompt.encode("utf-8")) > 512 * 1024):
        raise ValueError("launch.initial_prompt must be null or at most 512 KiB")
    if result["execution_lifetime"] not in {"live_control", "one_shot"}:
        raise ValueError("launch.execution_lifetime is not recognized")
    result["started_at"] = _parse_datetime(result["started_at"], "launch.started_at")
    result["expires_at"] = _parse_datetime(result["expires_at"], "launch.expires_at")
    if result["expires_at"] <= result["started_at"]:
        raise ValueError("launch.expires_at must be later than started_at")
    return result


def _validate_launch_outcome(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"state", "error_code", "error_message"}:
        raise ValueError("outcome has invalid fields")
    result = dict(value)
    if result["state"] not in {"dispatched", "adopted", "failed", "abandoned"}:
        raise ValueError("outcome.state is not recognized")
    for field, maximum in (("error_code", 64), ("error_message", 4096)):
        raw = result[field]
        if raw is not None and (not isinstance(raw, str) or not raw or len(raw) > maximum):
            raise ValueError(f"outcome.{field} must be null or contain 1 to {maximum} characters")
    return result


def _validate_heartbeat_stamp(value: dict) -> dict:
    if set(value) != _HEARTBEAT_FIELDS:
        raise ValueError("heartbeat has invalid fields")
    result = dict(value)
    if not _is_string(result["device_id"], maximum=255):
        raise ValueError("heartbeat.device_id must contain 1 to 255 characters")
    result["received_at"] = _parse_datetime(result["received_at"], "heartbeat.received_at")
    for field in ("last_ship_at", "last_ship_attempt_at"):
        if result[field] is not None:
            result[field] = _parse_datetime(result[field], f"heartbeat.{field}")
    for field in _HEARTBEAT_REQUIRED_INTEGER_FIELDS:
        if type(result[field]) is not int or result[field] < 0:
            raise ValueError(f"heartbeat.{field} must be a non-negative integer")
    for field in _HEARTBEAT_OPTIONAL_INTEGER_FIELDS:
        if result[field] is not None and (type(result[field]) is not int or result[field] < 0):
            raise ValueError(f"heartbeat.{field} must be a non-negative integer or null")
    if type(result["is_offline"]) is not int or result["is_offline"] not in (0, 1):
        raise ValueError("heartbeat.is_offline must be 0 or 1")
    for field, maximum in (("version", 50), ("last_ship_result", 64), ("sessions_digest", 128)):
        if result[field] is not None and (not isinstance(result[field], str) or not result[field] or len(result[field]) > maximum):
            raise ValueError(f"heartbeat.{field} must be null or contain 1 to {maximum} characters")
    if result["raw_json"] is not None and (not isinstance(result["raw_json"], str) or len(result["raw_json"].encode("utf-8")) > 512 * 1024):
        raise ValueError("heartbeat.raw_json must be null or at most 512 KiB")
    return result


def _validate_managed_lease(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _MANAGED_LEASE_FIELDS:
        raise ValueError("managed lease has invalid fields")
    result = dict(value)
    session_id = result["session_id"]
    try:
        parsed_session_id = uuid.UUID(session_id) if isinstance(session_id, str) else None
    except ValueError:
        parsed_session_id = None
    if parsed_session_id is None or str(parsed_session_id) != session_id:
        raise ValueError("managed lease session_id must be a canonical UUID")
    result["session_id"] = parsed_session_id
    for field, maximum, nullable in (
        ("provider", 64, False),
        ("machine_id", 255, True),
        ("state", 32, False),
        ("phase", 32, True),
        ("tool_name", 128, True),
        ("bridge_status", 64, True),
        ("thread_subscription_status", 64, True),
    ):
        raw = result[field]
        if raw is None and nullable:
            continue
        if not isinstance(raw, str) or not raw or len(raw) > maximum:
            suffix = " or null" if nullable else ""
            raise ValueError(f"managed lease {field} must contain 1 to {maximum} characters{suffix}")
    if type(result["sequence"]) is not int or result["sequence"] < 0:
        raise ValueError("managed lease sequence must be a non-negative integer")
    if type(result["lease_ttl_ms"]) is not int or not 1 <= result["lease_ttl_ms"] <= 3_600_000:
        raise ValueError("managed lease lease_ttl_ms must be an integer from 1 through 3600000")
    if result["observed_at"] is not None:
        result["observed_at"] = _parse_datetime(result["observed_at"], "managed lease observed_at")
    return result


def _validate_control_command_result(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("message must be an object")
    allowed = {"type", "command_id", "ok", "result", "error", "session_id"}
    if not set(value).issubset(allowed):
        raise ValueError("message has invalid fields")
    command_id = value.get("command_id")
    if not _is_string(command_id, maximum=96):
        raise ValueError("message.command_id must contain 1 to 96 characters")
    if type(value.get("ok")) is not bool:
        raise ValueError("message.ok must be a boolean")
    message_type = value.get("type")
    if message_type is not None and message_type != "command_result":
        raise ValueError("message.type must be command_result or null")
    for field in ("result", "error"):
        if value.get(field) is not None and not isinstance(value[field], dict):
            raise ValueError(f"message.{field} must be an object or null")
    session_id = value.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 64):
        raise ValueError("message.session_id must be a string of at most 64 characters or null")
    return {
        "type": "command_result",
        "command_id": command_id,
        "ok": value["ok"],
        "result": dict(value.get("result") or {}),
        "error": dict(value.get("error") or {}),
        "session_id": session_id,
    }


def socket_path_is_live(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_string(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= maximum


def _parse_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 UTC datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC datetime string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(UTC)
