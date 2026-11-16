"""Zstd compression helpers for raw_json archival columns.

Codec values stored in raw_json_codec:
  0 = PLAIN — original TEXT in raw_json column (all legacy rows)
  1 = ZSTD  — zstd-compressed bytes in raw_json_z BLOB; raw_json is '' / NULL sentinel

New rows always write codec=1 (compressed). Legacy rows stay codec=0 until the
background migration job backfills raw_json_z and clears raw_json text.
"""

from __future__ import annotations

import threading

import zstandard as zstd

CODEC_PLAIN = 0
CODEC_ZSTD = 1

_thread_state = threading.local()


def _get_compressor() -> zstd.ZstdCompressor:
    cctx = getattr(_thread_state, "compressor", None)
    if cctx is None:
        cctx = zstd.ZstdCompressor(level=3)
        _thread_state.compressor = cctx
    return cctx


def _get_decompressor() -> zstd.ZstdDecompressor:
    dctx = getattr(_thread_state, "decompressor", None)
    if dctx is None:
        dctx = zstd.ZstdDecompressor()
        _thread_state.decompressor = dctx
    return dctx


def compress_raw_json(text: str) -> bytes:
    """Compress a raw_json string to zstd bytes."""
    return _get_compressor().compress(text.encode())


def decompress_raw_json(blob: bytes) -> str:
    """Decompress a zstd BLOB back to a raw_json string."""
    return _get_decompressor().decompress(blob).decode()


def decode_raw_json(obj) -> str | None:
    """Return the raw_json string from an ORM row, decompressing transparently.

    Handles both codec=0 (plain TEXT in raw_json) and codec=1 (zstd BLOB in
    raw_json_z). Safe to call on any AgentEvent or AgentSourceLine instance.
    Returns None when no payload exists. Uses explicit None checks — never
    treats empty string as missing so legitimate empty values aren't lost.
    """
    codec = getattr(obj, "raw_json_codec", None)
    if codec == CODEC_ZSTD:
        blob = getattr(obj, "raw_json_z", None)
        if blob is None:
            return None
        return decompress_raw_json(blob)
    # CODEC_PLAIN or missing codec field — use text column as-is
    raw = getattr(obj, "raw_json", None)
    return raw  # may be None, empty string, or real content — caller decides
