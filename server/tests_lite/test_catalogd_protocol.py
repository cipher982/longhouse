import asyncio
import json
import struct

import pytest

from zerg.catalogd.protocol import HEADER_BYTES
from zerg.catalogd.protocol import MAGIC
from zerg.catalogd.protocol import MAX_PAYLOAD_BYTES
from zerg.catalogd.protocol import CatalogRpcError
from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import ProtocolError
from zerg.catalogd.protocol import decode_frame
from zerg.catalogd.protocol import encode_frame
from zerg.catalogd.protocol import read_frame

REQUEST_ID = "0123456789abcdef0123456789abcdef"


def _raw_frame(payload: bytes) -> bytes:
    return MAGIC + struct.pack(">I", len(payload)) + payload


def test_request_roundtrip() -> None:
    request = CatalogRpcRequest(
        id=REQUEST_ID,
        method="ping.v2",
        deadline_mono_ns="9876543210",
        params={"probe": "catalog"},
    )

    assert decode_frame(encode_frame(request)) == request


def test_success_and_error_response_roundtrip() -> None:
    success = CatalogRpcResponse(id=REQUEST_ID, result={"ok": True})
    failure = CatalogRpcResponse(
        id=REQUEST_ID,
        error=CatalogRpcError(
            code="catalog_unavailable",
            message="catalog is restarting",
            retryable=True,
            retry_after_ms=25,
            details={"phase": "recovery"},
        ),
    )

    assert decode_frame(encode_frame(success)) == success
    assert decode_frame(encode_frame(failure)) == failure


def test_rejects_malformed_magic() -> None:
    frame = encode_frame(CatalogRpcRequest(id=REQUEST_ID, method="ping.v2", deadline_mono_ns="1", params={}))

    with pytest.raises(ProtocolError, match="magic"):
        decode_frame(b"NOPE" + frame[4:])


def test_rejects_oversize_before_reading_payload() -> None:
    frame = MAGIC + struct.pack(">I", MAX_PAYLOAD_BYTES + 1)

    with pytest.raises(ProtocolError, match="1 MiB"):
        decode_frame(frame)


@pytest.mark.parametrize(
    "frame",
    [
        MAGIC[:2],
        MAGIC + b"\x00\x00",
        MAGIC + struct.pack(">I", 10) + b"{}",
    ],
)
def test_rejects_truncation(frame: bytes) -> None:
    with pytest.raises(ProtocolError, match="truncated"):
        decode_frame(frame)


def test_rejects_duplicate_keys() -> None:
    payload = b'{"v":2,"v":2,"id":"0123456789abcdef0123456789abcdef"}'

    with pytest.raises(ProtocolError, match="duplicate JSON key"):
        decode_frame(_raw_frame(payload))


@pytest.mark.parametrize("value", [[], "request", 2, None])
def test_rejects_nonobject_payload(value: object) -> None:
    with pytest.raises(ProtocolError, match="JSON object"):
        decode_frame(_raw_frame(json.dumps(value).encode()))


def test_rejects_unsupported_version() -> None:
    payload = json.dumps(
        {
            "v": 3,
            "id": REQUEST_ID,
            "method": "ping.v2",
            "deadline_mono_ns": "1",
            "params": {},
        }
    ).encode()

    with pytest.raises(ProtocolError) as exc_info:
        decode_frame(_raw_frame(payload))

    assert exc_info.value.code == "unsupported_version"


@pytest.mark.parametrize("deadline", [1, -1, "-1", "+1", "01", "1.0", "1e3", ""])
def test_rejects_noncanonical_deadline(deadline: object) -> None:
    payload = json.dumps(
        {
            "v": 2,
            "id": REQUEST_ID,
            "method": "ping.v2",
            "deadline_mono_ns": deadline,
            "params": {},
        }
    ).encode()

    with pytest.raises(ProtocolError, match="unsigned decimal string"):
        decode_frame(_raw_frame(payload))


def test_rejects_deadline_larger_than_u64() -> None:
    payload = json.dumps(
        {
            "v": 2,
            "id": REQUEST_ID,
            "method": "ping.v2",
            "deadline_mono_ns": str(1 << 64),
            "params": {},
        }
    ).encode()
    with pytest.raises(ProtocolError, match="exceeds u64"):
        decode_frame(_raw_frame(payload))


def test_frame_header_is_magic_then_u32be_payload_length() -> None:
    frame = encode_frame(CatalogRpcRequest(id=REQUEST_ID, method="ping.v2", deadline_mono_ns="0", params={}))

    assert frame[:4] == MAGIC
    assert struct.unpack(">I", frame[4:HEADER_BYTES])[0] == len(frame) - HEADER_BYTES


@pytest.mark.asyncio
async def test_stream_reader_reads_exactly_one_frame() -> None:
    request = CatalogRpcRequest(id=REQUEST_ID, method="ping.v2", deadline_mono_ns="1", params={})
    reader = asyncio.StreamReader()
    reader.feed_data(encode_frame(request))
    reader.feed_data(encode_frame(request))
    reader.feed_eof()

    assert await read_frame(reader) == request
    assert await read_frame(reader) == request


@pytest.mark.asyncio
async def test_stream_reader_rejects_truncated_payload() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(MAGIC + struct.pack(">I", 5) + b"{}")
    reader.feed_eof()

    with pytest.raises(ProtocolError, match="truncated frame payload"):
        await read_frame(reader)
