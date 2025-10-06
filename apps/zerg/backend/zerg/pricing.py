"""Pricing catalog for LLM token costs (per 1K tokens).

Only models explicitly listed here have cost computed. Unknown models result
in a `None` cost and a structured log entry – no estimation or fallback.
"""

from __future__ import annotations

from typing import Optional
from typing import Tuple

# USD per 1K tokens (in, out).
# Default catalog is intentionally minimal (mock only). Real prices are
# loaded from an external JSON catalog referenced via env.
MODEL_PRICES_USD_PER_1K: dict[str, Tuple[float, float]] = {
    "gpt-mock": (0.0, 0.0),
}

_CATALOG_CACHE: Optional[dict[str, Tuple[float, float]]] = None


def _load_from_env() -> Optional[dict[str, Tuple[float, float]]]:
    """Load pricing from JSON file specified by PRICING_CATALOG_PATH.

    Accepted JSON shapes:
    - { "model_id": [in_price_per_1k, out_price_per_1k], ... }
    - { "model_id": {"in": 0.001, "out": 0.002}, ... }
    Returns None if no file or invalid content.
    """
    import json
    import os
    from pathlib import Path

    path = os.getenv("PRICING_CATALOG_PATH") or os.getenv("PRICING_CATALOG_JSON")
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, Tuple[float, float]] = {}
    for k, v in raw.items():
        try:
            if isinstance(v, (list, tuple)) and len(v) == 2:
                parsed[k] = (float(v[0]), float(v[1]))
            elif isinstance(v, dict) and "in" in v and "out" in v:
                parsed[k] = (float(v["in"]), float(v["out"]))
        except Exception:
            # Skip invalid entry
            continue
    return parsed or None


def get_usd_prices_per_1k(model_id: str) -> Optional[Tuple[float, float]]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        external = _load_from_env()
        _CATALOG_CACHE = {**MODEL_PRICES_USD_PER_1K, **(external or {})}
    return _CATALOG_CACHE.get(model_id)
