"""zstd / gzip bomb hardening for /agents/ingest decompression."""

from __future__ import annotations

import gzip
import os

import pytest
import zstandard
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers.agents_ingest import (
    MAX_DECOMPRESSED_BODY_BYTES,
    _decompress_bounded_gzip,
    _decompress_bounded_zstd,
)


def _zeros(n: int) -> bytes:
    return b"\x00" * n


def test_zstd_under_cap_decompresses_normally():
    payload = _zeros(64 * 1024)
    compressed = zstandard.ZstdCompressor(level=1).compress(payload)
    assert _decompress_bounded_zstd(compressed) == payload


def test_gzip_under_cap_decompresses_normally():
    payload = _zeros(64 * 1024)
    compressed = gzip.compress(payload)
    assert _decompress_bounded_gzip(compressed) == payload


def test_zstd_bomb_is_rejected_with_413():
    # ~16 MiB compressed → ~1 GiB decompressed. zstd compresses zeros
    # extremely well, so this is the canonical bomb shape.
    bomb_size = MAX_DECOMPRESSED_BODY_BYTES * 4
    compressed = zstandard.ZstdCompressor(level=1).compress(_zeros(bomb_size))
    assert len(compressed) < MAX_DECOMPRESSED_BODY_BYTES // 4, (
        "test setup: bomb must be far smaller than the decompressed cap"
    )
    with pytest.raises(HTTPException) as exc:
        _decompress_bounded_zstd(compressed)
    assert exc.value.status_code == 413
    assert "exceeds" in str(exc.value.detail).lower()


def test_gzip_bomb_is_rejected_with_413():
    bomb_size = MAX_DECOMPRESSED_BODY_BYTES * 4
    compressed = gzip.compress(_zeros(bomb_size), compresslevel=9)
    assert len(compressed) < MAX_DECOMPRESSED_BODY_BYTES // 4
    with pytest.raises(HTTPException) as exc:
        _decompress_bounded_gzip(compressed)
    assert exc.value.status_code == 413
