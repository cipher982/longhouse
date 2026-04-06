#!/usr/bin/env python3
"""Real-call smoke for the AI ops watchman Grok 4.1 path.

Calls OpenRouter-routed ``x-ai/grok-4.1-fast`` with a tiny watchman-style
prompt, requires JSON output, and prints token/cost metadata so we can validate
the provider path before wiring the full feature into the app.

Usage:
    OPENROUTER_API_KEY=... python scripts/qa/ops_watchman_grok_smoke.py
    python scripts/qa/ops_watchman_grok_smoke.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from zerg.pricing import get_usd_prices_per_1k  # noqa: E402

MODEL_ID = "x-ai/grok-4.1-fast"
BASE_URL = "https://openrouter.ai/api/v1"
ALLOWED_STATUSES = {"normal", "watch", "critical"}
SYSTEM_PROMPT = """You are Longhouse AI Ops Watchman.

You analyze recent raw operational observations and decide whether the system
story looks normal or dangerous.

Return valid JSON with exactly these keys:
- status: normal | watch | critical
- title: short string
- summary: short string
- evidence: array of short strings
- should_email: boolean
- recommended_action: short string

Rules:
- Be skeptical.
- Do not invent facts not present in the observations.
- Prefer normal if the evidence is weak.
- Escalate only when the observations clearly support concern.
"""


def _default_observations() -> list[dict[str, Any]]:
    return [
        {
            "observed_at": "2026-03-28T19:30:00Z",
            "entity_type": "tenant",
            "entity_id": "david010",
            "source": "db_file_stats",
            "payload": {
                "db_bytes": 33_812_983_808,
                "wal_bytes": 12_442_345_472,
                "window": "5m",
            },
        },
        {
            "observed_at": "2026-03-28T19:30:05Z",
            "entity_type": "session",
            "entity_id": "019d1805-66b6-78f1-aca9-91225867663d",
            "source": "session_growth",
            "payload": {
                "ended_at": "2026-03-24T03:41:00Z",
                "new_events_10m": 124_550,
                "user_messages": 1_974,
                "assistant_messages": 9_553,
                "tool_calls": 39_722,
            },
        },
        {
            "observed_at": "2026-03-28T19:30:08Z",
            "entity_type": "session",
            "entity_id": "019d1805-66b6-78f1-aca9-91225867663d",
            "source": "session_lineage",
            "payload": {
                "branch_count": 13,
                "distinct_source_paths": 46,
                "rewrite_branches": 12,
            },
        },
        {
            "observed_at": "2026-03-28T19:30:10Z",
            "entity_type": "tenant",
            "entity_id": "david010",
            "source": "serializer_pressure",
            "payload": {
                "avg_queue_wait_ms": 124_000,
                "max_queue_wait_ms": 311_000,
                "ingest_timeouts_5m": 18,
            },
        },
    ]


def _load_observations(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return _default_observations()
    raw = Path(path).read_text()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Observation input must be a JSON array")
    return data


def _build_request(model_id: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "window": "recent",
                        "observations": observations,
                    },
                    ensure_ascii=True,
                ),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 512,
    }


def _extract_reasoning_tokens(usage: Any) -> int | None:
    details = getattr(usage, "completion_tokens_details", None)
    if details is None:
        return None
    return getattr(details, "reasoning_tokens", None)


def _estimate_cost_usd(model_id: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    prices = get_usd_prices_per_1k(model_id)
    if not prices:
        return None
    in_price, out_price = prices
    return round(((input_tokens * in_price) + (output_tokens * out_price)) / 1000.0, 8)


def _validate_result(payload: dict[str, Any]) -> None:
    missing = {"status", "title", "summary", "evidence", "should_email", "recommended_action"} - set(payload.keys())
    if missing:
        raise ValueError(f"Smoke response missing keys: {sorted(missing)}")
    status = str(payload.get("status") or "").strip().lower()
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unexpected status: {status!r}")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("Smoke response 'evidence' must be a list")
    if not isinstance(payload.get("should_email"), bool):
        raise ValueError("Smoke response 'should_email' must be a boolean")


async def _run(model_id: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is required. Example: "
            'OPENROUTER_API_KEY="$(python3 ~/git/me/scripts/infisical-get.py OPENROUTER_API_KEY --project personal-shell --env dev)"'
        )

    client = AsyncOpenAI(api_key=api_key, base_url=BASE_URL, timeout=30.0)
    started = time.monotonic()
    try:
        response = await client.chat.completions.create(**_build_request(model_id, observations))
    finally:
        await client.close()

    elapsed_ms = round((time.monotonic() - started) * 1000)
    content = response.choices[0].message.content or ""
    if not content:
        raise RuntimeError("Smoke call returned empty content")

    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("Smoke response JSON must be an object")
    _validate_result(parsed)

    usage = response.usage
    input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    output_tokens = getattr(usage, "completion_tokens", None) if usage else None
    total_tokens = getattr(usage, "total_tokens", None) if usage else None
    reasoning_tokens = _extract_reasoning_tokens(usage) if usage else None
    provider_cost_ticks = getattr(usage, "cost_in_usd_ticks", None) if usage else None

    return {
        "model": model_id,
        "base_url": BASE_URL,
        "duration_ms": elapsed_ms,
        "result": parsed,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "reasoning_tokens": reasoning_tokens,
            "provider_cost_in_usd_ticks": provider_cost_ticks,
            "estimated_cost_usd": _estimate_cost_usd(model_id, input_tokens, output_tokens),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the AI ops watchman prompt against OpenRouter-routed Grok 4.1.")
    parser.add_argument("--json", action="store_true", help="Print raw JSON result")
    parser.add_argument("--model", default=MODEL_ID, help=f"Model id (default: {MODEL_ID})")
    parser.add_argument("--observations", help="Optional JSON file with observation array")
    args = parser.parse_args()

    result = asyncio.run(_run(args.model, _load_observations(args.observations)))

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    print(f"model: {result['model']}")
    print(f"duration_ms: {result['duration_ms']}")
    print(f"status: {result['result']['status']}")
    print(f"title: {result['result']['title']}")
    print(f"summary: {result['result']['summary']}")
    print(f"should_email: {result['result']['should_email']}")
    print(f"recommended_action: {result['result']['recommended_action']}")
    print("evidence:")
    for item in result["result"]["evidence"]:
        print(f"  - {item}")
    print("usage:")
    for key, value in result["usage"].items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
