"""Strict, dependency-free framing for catalogd's local Unix RPC boundary."""

from __future__ import annotations

import json
import re
import struct
from asyncio import IncompleteReadError
from asyncio import StreamReader
from asyncio import StreamWriter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import TypeAlias

MAGIC = b"LHR2"
VERSION = 2
MAX_PAYLOAD_BYTES = 1024 * 1024
HEADER_BYTES = len(MAGIC) + 4

_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_UNSIGNED_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_METHOD_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\.v2\Z")

ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unsupported_version",
        "unknown_method",
        "deadline_exceeded",
        "catalog_unavailable",
        "schema_incompatible",
        "internal",
        "source_epoch_conflict",
        "session_deleted",
        "stale_generation",
        "invalid_cursor",
        "projection_lag",
        "conflict",
        "resource_exhausted",
    }
)


class ProtocolError(ValueError):
    """A malformed or unsupported catalogd wire message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CatalogRpcRequest:
    id: str
    method: str
    deadline_mono_ns: str
    params: dict[str, Any]
    v: int = VERSION

    def to_wire(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "id": self.id,
            "method": self.method,
            "deadline_mono_ns": self.deadline_mono_ns,
            "params": self.params,
        }


@dataclass(frozen=True, slots=True)
class CatalogRpcError:
    code: str
    message: str
    retryable: bool
    retry_after_ms: int | None
    details: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class CatalogRpcResponse:
    id: str
    result: dict[str, Any] | None = None
    error: CatalogRpcError | None = None
    v: int = VERSION

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("response must contain exactly one of result or error")

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"v": self.v, "id": self.id}
        if self.error is not None:
            wire["error"] = self.error.to_wire()
        else:
            wire["result"] = self.result
        return wire


CatalogRpcMessage: TypeAlias = CatalogRpcRequest | CatalogRpcResponse


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise ProtocolError("invalid_request", f"duplicate JSON key: {key}")
        decoded[key] = value
    return decoded


def _reject_non_json_constant(value: str) -> None:
    raise ProtocolError("invalid_request", f"non-JSON numeric constant: {value}")


def decode_payload(payload: bytes) -> dict[str, Any]:
    """Decode one strict UTF-8 JSON object without accepting duplicate keys."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid_request", "payload is not valid UTF-8") from exc
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except ProtocolError:
        raise
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid_request", "payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("invalid_request", "payload must be a JSON object")
    return decoded


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, subject: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProtocolError("invalid_request", f"invalid {subject} fields: missing={missing}, extra={extra}")


def _parse_version(value: Any) -> None:
    if type(value) is not int:
        raise ProtocolError("invalid_request", "v must be an integer")
    if value != VERSION:
        raise ProtocolError("unsupported_version", f"unsupported protocol version: {value}")


def _parse_request_id(value: Any) -> str:
    if not isinstance(value, str) or _REQUEST_ID_RE.fullmatch(value) is None:
        raise ProtocolError("invalid_request", "id must be 32 lowercase hexadecimal characters")
    return value


def _parse_object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_request", f"{field} must be a JSON object")
    return value


def _parse_request(wire: dict[str, Any]) -> CatalogRpcRequest:
    _require_exact_keys(wire, {"v", "id", "method", "deadline_mono_ns", "params"}, subject="request")
    method = wire["method"]
    if not isinstance(method, str) or len(method) > 128 or _METHOD_RE.fullmatch(method) is None:
        raise ProtocolError("invalid_request", "method must be a versioned lowercase .v2 name")
    deadline = wire["deadline_mono_ns"]
    if not isinstance(deadline, str) or _UNSIGNED_DECIMAL_RE.fullmatch(deadline) is None:
        raise ProtocolError("invalid_request", "deadline_mono_ns must be an unsigned decimal string")
    if int(deadline) >= 1 << 64:
        raise ProtocolError("invalid_request", "deadline_mono_ns exceeds u64")
    return CatalogRpcRequest(
        v=wire["v"],
        id=_parse_request_id(wire["id"]),
        method=method,
        deadline_mono_ns=deadline,
        params=_parse_object(wire["params"], field="params"),
    )


def _parse_error(value: Any) -> CatalogRpcError:
    wire = _parse_object(value, field="error")
    _require_exact_keys(wire, {"code", "message", "retryable", "retry_after_ms", "details"}, subject="error")
    code = wire["code"]
    if not isinstance(code, str) or code not in ERROR_CODES:
        raise ProtocolError("invalid_request", "error.code is not a recognized catalogd error code")
    message = wire["message"]
    if not isinstance(message, str):
        raise ProtocolError("invalid_request", "error.message must be a string")
    retryable = wire["retryable"]
    if type(retryable) is not bool:
        raise ProtocolError("invalid_request", "error.retryable must be a boolean")
    retry_after_ms = wire["retry_after_ms"]
    if retry_after_ms is not None and (type(retry_after_ms) is not int or retry_after_ms < 0):
        raise ProtocolError("invalid_request", "error.retry_after_ms must be a non-negative integer or null")
    return CatalogRpcError(
        code=code,
        message=message,
        retryable=retryable,
        retry_after_ms=retry_after_ms,
        details=_parse_object(wire["details"], field="error.details"),
    )


def parse_message(wire: dict[str, Any]) -> CatalogRpcMessage:
    """Validate and convert a decoded wire object to its typed representation."""

    _parse_version(wire.get("v"))
    if "method" in wire:
        return _parse_request(wire)
    if "result" in wire:
        _require_exact_keys(wire, {"v", "id", "result"}, subject="success response")
        return CatalogRpcResponse(
            v=wire["v"],
            id=_parse_request_id(wire["id"]),
            result=_parse_object(wire["result"], field="result"),
        )
    if "error" in wire:
        _require_exact_keys(wire, {"v", "id", "error"}, subject="error response")
        return CatalogRpcResponse(v=wire["v"], id=_parse_request_id(wire["id"]), error=_parse_error(wire["error"]))
    raise ProtocolError("invalid_request", "message is neither a request nor a response")


def encode_frame(message: CatalogRpcMessage) -> bytes:
    """Encode one typed RPC message as ``LHR2 + u32be length + JSON``."""

    wire = message.to_wire()
    parse_message(wire)
    payload = json.dumps(wire, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("invalid_request", "payload exceeds the 1 MiB frame limit")
    return MAGIC + struct.pack(">I", len(payload)) + payload


def decode_frame(frame: bytes) -> CatalogRpcMessage:
    """Decode exactly one complete frame; trailing or truncated data is invalid."""

    if len(frame) < HEADER_BYTES:
        raise ProtocolError("invalid_request", "truncated frame header")
    if frame[: len(MAGIC)] != MAGIC:
        raise ProtocolError("invalid_request", "invalid frame magic")
    payload_length = struct.unpack(">I", frame[len(MAGIC) : HEADER_BYTES])[0]
    if payload_length > MAX_PAYLOAD_BYTES:
        raise ProtocolError("invalid_request", "payload exceeds the 1 MiB frame limit")
    if len(frame) != HEADER_BYTES + payload_length:
        description = "truncated frame payload" if len(frame) < HEADER_BYTES + payload_length else "frame has trailing bytes"
        raise ProtocolError("invalid_request", description)
    return parse_message(decode_payload(frame[HEADER_BYTES:]))


async def read_frame(reader: StreamReader) -> CatalogRpcMessage:
    """Read one bounded frame from a stream without buffering an oversized body."""

    try:
        header = await reader.readexactly(HEADER_BYTES)
    except IncompleteReadError as exc:
        raise ProtocolError("invalid_request", "truncated frame header") from exc
    if header[: len(MAGIC)] != MAGIC:
        raise ProtocolError("invalid_request", "invalid frame magic")
    payload_length = struct.unpack(">I", header[len(MAGIC) :])[0]
    if payload_length > MAX_PAYLOAD_BYTES:
        raise ProtocolError("invalid_request", "payload exceeds the 1 MiB frame limit")
    try:
        payload = await reader.readexactly(payload_length)
    except IncompleteReadError as exc:
        raise ProtocolError("invalid_request", "truncated frame payload") from exc
    return parse_message(decode_payload(payload))


async def write_frame(writer: StreamWriter, message: CatalogRpcMessage) -> None:
    writer.write(encode_frame(message))
    await writer.drain()
