"""JSON decoding for the ingest hot path.

The standard-library decoder was 28% of the serve process's interpreter time
during a bulk import (py-spy --gil, 2026-11-12): the same raw provider records
are parsed while validating the envelope, while enriching render semantics and
again in the semantic projector. msgspec decodes captured envelopes 7x and raw
records 2x faster, with identical values on 69 envelopes and 16,127 records.

msgspec is stricter than ``json``: it raises on NaN/Infinity, lone surrogates,
out-of-range floats and UTF-16/32 byte input. Any input it rejects is decoded
again by ``json.loads``, so values and raised exceptions match ``json.loads``
exactly. (orjson was rejected: it turns integers beyond 64 bits into floats
without raising.)
"""

from __future__ import annotations

import json
from typing import Any

try:
    import msgspec
except ImportError:  # the qualification router imports this module without the server's optional dependencies
    msgspec = None  # type: ignore[assignment]

_decoder = msgspec.json.Decoder() if msgspec is not None else None


def loads(data: str | bytes | bytearray | memoryview) -> Any:
    """Decode like ``json.loads(data)``, using msgspec when it accepts the input."""

    if _decoder is None:
        return json.loads(data)
    try:
        return _decoder.decode(data)
    except msgspec.DecodeError:
        return json.loads(data)
