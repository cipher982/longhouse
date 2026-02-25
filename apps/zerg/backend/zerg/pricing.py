"""Pricing catalog for LLM token costs (per 1K tokens).

Only models explicitly listed here have cost computed. Unknown models result
in a `None` cost and a structured log entry – no estimation or fallback.
"""

from __future__ import annotations

from typing import Optional
from typing import Tuple

# USD per 1K tokens (in, out).
# Built-in prices are baked in so cost tracking works without external config.
# Prices are approximate and may drift — override via PRICING_CATALOG_PATH for accuracy.
# External catalog is merged on top of these defaults, so overrides win.
MODEL_PRICES_USD_PER_1K: dict[str, Tuple[float, float]] = {
    "gpt-mock": (0.0, 0.0),
    # Groq (shared-pool defaults for hosted instances) — prices per 1K tokens
    # Source: groq.com/pricing (verify periodically; these are approximate 2025 rates)
    "qwen/qwen3-32b": (0.00029, 0.00059),
    "meta-llama/llama-4-maverick-17b-128e-instruct": (0.0002, 0.0006),
    "llama-3.3-70b-versatile": (0.00059, 0.00079),
    "llama-3.1-8b-instant": (0.00005, 0.00008),
    # OpenAI (added when OpenAI key is provisioned) — approximate 2025 rates
    # These are placeholders; actual gpt-5.x pricing should be loaded via PRICING_CATALOG_PATH
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
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
