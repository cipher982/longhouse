"""Zstd compression helpers for raw_json archival columns.

Codec values stored in raw_json_codec:
  0 = PLAIN — original TEXT in raw_json column (all legacy rows)
  1 = ZSTD  — zstd-compressed bytes in raw_json_z BLOB; raw_json is '' / NULL sentinel

New rows always write codec=1 (compressed). Legacy rows stay codec=0 until the
background migration job backfills raw_json_z and nulls/clears raw_json.
"""

from __future__ import annotations

import zstandard as zstd

CODEC_PLAIN = 0
CODEC_ZSTD = 1

# Module-level compressor/decompressor — thread-safe for reads; writes go
# through the single WriteSerializer so no concurrent compression contention.
_cctx = zstd.ZstdCompressor(level=3)
_dctx = zstd.ZstdDecompressor()


def compress_raw_json(text: str) -> bytes:
    """Compress a raw_json string to zstd bytes."""
    return _cctx.compress(text.encode())


def decompress_raw_json(blob: bytes) -> str:
    """Decompress a zstd BLOB back to a raw_json string."""
    return _dctx.decompress(blob).decode()


def decode_raw_json(obj) -> str | None:
    """Return the raw_json string from an ORM row, decompressing transparently.

    Handles both codec=0 (plain TEXT in raw_json) and codec=1 (zstd BLOB in
    raw_json_z). Safe to call on any AgentEvent or AgentSourceLine instance.
    Returns None when neither column has data (e.g. event had no raw payload).
    """
    codec = getattr(obj, "raw_json_codec", None)
    if codec == CODEC_ZSTD:
        blob = getattr(obj, "raw_json_z", None)
        if not blob:
            return None
        return decompress_raw_json(blob)
    # CODEC_PLAIN or missing codec field — fall back to text column
    raw = getattr(obj, "raw_json", None)
    return raw if raw else None
