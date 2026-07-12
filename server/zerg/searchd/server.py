"""Unix-socket daemon that exclusively owns the disposable search database."""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

from zerg.catalogd.protocol import CatalogRpcError
from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import ProtocolError
from zerg.catalogd.protocol import read_frame
from zerg.catalogd.protocol import write_frame
from zerg.searchd.store import SearchStore
from zerg.searchd.store import WorklogPageTooLarge
from zerg.searchd.store import WorklogSnapshotError
from zerg.searchd.store import open_search_database

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_ROLES = {"user", "assistant", "tool", "system"}


class SearchDaemon:
    def __init__(self, *, database_path: Path, socket_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.socket_path = socket_path.expanduser().resolve()
        self.lock_path = self.database_path.with_suffix(f"{self.database_path.suffix}.searchd.lock")
        self._lock_handle = None
        self._connection = None
        self._read_connection = None
        self._store: SearchStore | None = None
        self._read_store: SearchStore | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._read_executor: ThreadPoolExecutor | None = None
        self._server: asyncio.AbstractServer | None = None
        self._published_inode: tuple[int, int] | None = None

    async def start(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.socket_path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
            raise RuntimeError("searchd socket parent is unsafe")
        os.chmod(self.socket_path.parent, 0o700)
        self._acquire_lock()
        try:
            self._connection = open_search_database(self.database_path)
            self._store = SearchStore(self._connection)
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="searchd-sqlite")
            self._read_connection = open_search_database(self.database_path)
            self._read_store = SearchStore(self._read_connection)
            self._read_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="searchd-read")
            self._prepare_socket()
            temporary = self.socket_path.with_name(f".{self.socket_path.name}.tmp.{os.getpid()}")
            if len(os.fsencode(temporary)) >= 104:
                raise RuntimeError("searchd socket path exceeds the portable Unix limit")
            temporary.unlink(missing_ok=True)
            self._server = await asyncio.start_unix_server(self._handle_connection, path=temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.socket_path)
            published = self.socket_path.stat()
            self._published_inode = (published.st_dev, published.st_ino)
        except BaseException:
            await self.close()
            raise

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("searchd is not started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._unlink_socket()
        self._store = None
        self._read_store = None
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        if self._read_executor is not None:
            self._read_executor.shutdown(wait=True, cancel_futures=True)
            self._read_executor = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._read_connection is not None:
            self._read_connection.close()
            self._read_connection = None
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def _acquire_lock(self) -> None:
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("searchd database is already owned") from exc
        self._lock_handle = handle

    def _prepare_socket(self) -> None:
        try:
            entry = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid():
            raise RuntimeError("searchd socket path is unsafe")
        self.socket_path.unlink()

    def _unlink_socket(self) -> None:
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
                request = await read_frame(reader)
                if not isinstance(request, CatalogRpcRequest):
                    raise ProtocolError("invalid_request", "searchd accepts request frames only")
                response = await self._dispatch(request)
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
        if self._store is None or self._executor is None or self._read_store is None or self._read_executor is None:
            return self._error(request, "catalog_unavailable", "search index is not ready", retryable=True)
        try:
            if request.method == "search.ping.v2":
                ping = await self._run_read(self._read_store.ping)
                return self._result(request, {**ping, "pid": os.getpid()})
            if request.method == "search.index.object.v2":
                params = _index_object_params(request.params)
                return self._result(request, await self._run(self._store.index_object, **params))
            if request.method == "search.index.publish.v2":
                params = _publish_params(request.params)
                return self._result(request, await self._run(self._store.publish_generation, **params))
            if request.method == "search.query.v2":
                params = _search_params(request.params)
                return self._result(request, await self._run_read(self._read_store.search, **params))
            if request.method == "worklog.day.v2":
                params = _worklog_params(request.params)
                return self._result(request, await self._run_read(self._read_store.worklog_day, **params))
            if request.method == "worklog.snapshot.release.v2":
                _exact_keys(request.params, {"snapshot_id", "owner_id"})
                return self._result(
                    request,
                    await self._run_read(
                        self._read_store.release_worklog_snapshot,
                        snapshot_id=_uuid(request.params["snapshot_id"], "snapshot_id"),
                        owner_id=_text(request.params["owner_id"], "owner_id", 64),
                    ),
                )
            if request.method == "search.session.delete.v2":
                _exact_keys(request.params, {"session_id"})
                return self._result(
                    request,
                    await self._run(self._store.delete_session, session_id=_uuid(request.params["session_id"], "session_id")),
                )
            return self._error(request, "unknown_method", "searchd method is unknown")
        except ValueError as exc:
            return self._error(request, "invalid_request", str(exc))
        except WorklogPageTooLarge as exc:
            return self._error(request, "record_too_large", str(exc))
        except WorklogSnapshotError as exc:
            return self._error(request, exc.code, str(exc), retryable=exc.code in {"snapshot_capacity", "stale_snapshot"})
        except Exception:
            return self._error(request, "internal", "searchd operation failed")

    async def _run(self, function, **kwargs):
        assert self._executor is not None
        return await asyncio.get_running_loop().run_in_executor(self._executor, lambda: function(**kwargs))

    async def _run_read(self, function, **kwargs):
        assert self._read_executor is not None
        return await asyncio.get_running_loop().run_in_executor(self._read_executor, lambda: function(**kwargs))

    @staticmethod
    def _result(request: CatalogRpcRequest, result: dict[str, object]) -> CatalogRpcResponse:
        return CatalogRpcResponse(id=request.id, result=result)

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
            error=CatalogRpcError(code=code, message=message, retryable=retryable, retry_after_ms=None, details={}),
        )


def _exact_keys(value: dict, expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("request fields do not match the searchd contract")


def _text(value: object, field: str, maximum: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value.encode()) > maximum:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return value


def _uuid(value: object, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return str(parsed)


def _revision(value: object, field: str) -> int:
    if not isinstance(value, str) or not value.isdecimal() or int(value) >= 1 << 63:
        raise ValueError(f"{field} must be a decimal i63 string")
    return int(value)


def _index_object_params(value: dict) -> dict:
    expected = {
        "session_id",
        "generation_id",
        "object_id",
        "desired_revision",
        "provider",
        "machine_id",
        "project",
        "environment",
        "cwd",
        "git_repo",
        "opaque_source_id",
        "source_epoch",
        "records",
    }
    _exact_keys(value, expected)
    object_id = value["object_id"]
    if not isinstance(object_id, str) or _HASH.fullmatch(object_id) is None:
        raise ValueError("object_id must be a lowercase SHA-256 hash")
    provider = _text(value["provider"], "provider", 32)
    if _PROVIDER.fullmatch(provider) is None:
        raise ValueError("provider is not canonical")
    records = value["records"]
    if not isinstance(records, list) or len(records) > 10_000:
        raise ValueError("records must be a bounded list")
    parsed_records = []
    expected_record_fields = {
        "event_id",
        "record_ordinal",
        "order_time_us",
        "source_position",
        "event_subordinal",
        "role",
        "content_text",
        "tool_name",
        "tool_output_text",
        "tool_call_id",
        "thread_id",
        "branch_kind",
    }
    for record in records:
        if not isinstance(record, dict) or set(record) != expected_record_fields:
            raise ValueError("search record fields are invalid")
        if (
            not isinstance(record["event_id"], str)
            or not 0 < len(record["event_id"].encode()) <= 255
            or type(record["record_ordinal"]) is not int
            or not 0 <= record["record_ordinal"] < 10_000
            or type(record["order_time_us"]) is not int
            or not -(1 << 63) <= record["order_time_us"] < 1 << 63
            or type(record["source_position"]) is not int
            or not 0 <= record["source_position"] < 1 << 64
            or type(record["event_subordinal"]) is not int
            or not 0 <= record["event_subordinal"] < 1 << 32
            or record["role"] not in _ROLES
        ):
            raise ValueError("search record identity is invalid")
        for field, maximum in (
            ("content_text", 2_097_152),
            ("tool_name", 255),
            ("tool_output_text", 2_097_152),
            ("tool_call_id", 255),
            ("thread_id", 255),
            ("branch_kind", 64),
        ):
            item = record[field]
            if item is not None and (not isinstance(item, str) or len(item.encode()) > maximum):
                raise ValueError(f"search record {field} is invalid")
        parsed_records.append(dict(record))
    return {
        "session_id": _uuid(value["session_id"], "session_id"),
        "generation_id": _uuid(value["generation_id"], "generation_id"),
        "object_id": object_id,
        "desired_revision": _revision(value["desired_revision"], "desired_revision"),
        "provider": provider,
        "machine_id": _text(value["machine_id"], "machine_id", 255),
        "project": _text(value["project"], "project", 255, optional=True),
        "environment": _text(value["environment"], "environment", 32),
        "cwd": _text(value["cwd"], "cwd", 4_096, optional=True),
        "git_repo": _text(value["git_repo"], "git_repo", 500, optional=True),
        "opaque_source_id": _text(value["opaque_source_id"], "opaque_source_id", 4_096),
        "source_epoch": _uuid(value["source_epoch"], "source_epoch"),
        "records": parsed_records,
    }


def _publish_params(value: dict) -> dict:
    expected = {
        "session_id",
        "generation_id",
        "owner_id",
        "desired_revision",
        "object_count",
        "object_set_hash",
        "event_count",
        "project",
        "provider",
        "environment",
        "cwd",
        "git_repo",
        "started_at",
    }
    _exact_keys(value, expected)
    for field in ("object_count", "event_count"):
        if type(value[field]) is not int or not 0 <= value[field] <= 1_000_000_000:
            raise ValueError(f"{field} is invalid")
    object_set_hash = value["object_set_hash"]
    if not isinstance(object_set_hash, str) or _HASH.fullmatch(object_set_hash) is None:
        raise ValueError("object_set_hash must be a lowercase SHA-256 hash")
    provider = _text(value["provider"], "provider", 32)
    if _PROVIDER.fullmatch(provider) is None:
        raise ValueError("provider is not canonical")
    return {
        "session_id": _uuid(value["session_id"], "session_id"),
        "generation_id": _uuid(value["generation_id"], "generation_id"),
        "owner_id": _text(value["owner_id"], "owner_id", 64),
        "desired_revision": _revision(value["desired_revision"], "desired_revision"),
        "object_count": value["object_count"],
        "object_set_hash": object_set_hash,
        "event_count": value["event_count"],
        "project": _text(value["project"], "project", 255, optional=True),
        "provider": provider,
        "environment": _text(value["environment"], "environment", 32),
        "cwd": _text(value["cwd"], "cwd", 4_096, optional=True),
        "git_repo": _text(value["git_repo"], "git_repo", 500, optional=True),
        "started_at": _text(value["started_at"], "started_at", 64),
    }


def _search_params(value: dict) -> dict:
    _exact_keys(
        value,
        {
            "owner_id",
            "query",
            "project",
            "provider",
            "environment",
            "window_start_us",
            "window_end_us",
            "limit",
        },
    )
    if type(value["limit"]) is not int or not 1 <= value["limit"] <= 200:
        raise ValueError("limit is invalid")
    for field in ("window_start_us", "window_end_us"):
        item = value[field]
        if item is not None and (type(item) is not int or not -(1 << 63) <= item < 1 << 63):
            raise ValueError(f"{field} is invalid")
    if value["window_start_us"] is not None and value["window_end_us"] is not None:
        if value["window_start_us"] >= value["window_end_us"]:
            raise ValueError("search time window is empty")
    provider = _text(value["provider"], "provider", 32, optional=True)
    if provider is not None and _PROVIDER.fullmatch(provider) is None:
        raise ValueError("provider is not canonical")
    return {
        "owner_id": _text(value["owner_id"], "owner_id", 64),
        "query": _text(value["query"], "query", 1_000),
        "project": _text(value["project"], "project", 255, optional=True),
        "provider": provider,
        "environment": _text(value["environment"], "environment", 32, optional=True),
        "window_start_us": value["window_start_us"],
        "window_end_us": value["window_end_us"],
        "limit": value["limit"],
    }


def _worklog_params(value: dict) -> dict:
    _exact_keys(
        value,
        {
            "owner_id",
            "window_start_us",
            "window_end_us",
            "include_test",
            "section",
            "snapshot_id",
            "offset",
            "limit",
        },
    )
    if (
        type(value["window_start_us"]) is not int
        or type(value["window_end_us"]) is not int
        or value["window_start_us"] >= value["window_end_us"]
        or type(value["include_test"]) is not bool
        or type(value["limit"]) is not int
        or not 1 <= value["limit"] <= 500
        or value["section"] not in {"sessions", "events"}
        or type(value["offset"]) is not int
        or not 0 <= value["offset"] <= 100_000
    ):
        raise ValueError("worklog window is invalid")
    snapshot_value = value["snapshot_id"]
    snapshot_id = _uuid(snapshot_value, "snapshot_id") if snapshot_value is not None else None
    return {
        "owner_id": _text(value["owner_id"], "owner_id", 64),
        "window_start_us": value["window_start_us"],
        "window_end_us": value["window_end_us"],
        "include_test": value["include_test"],
        "section": value["section"],
        "snapshot_id": snapshot_id,
        "offset": value["offset"],
        "limit": value["limit"],
    }


__all__ = ["SearchDaemon"]
