#!/usr/bin/env python3
"""Smoke test every model in config/models.json with a trivial API call.

Text models: "What is 2+2?" (max_tokens=5)
Embedding models: embed the word "test"
Anthropic models: uses Anthropic SDK (messages API)

Skips models whose API key env var is not set.
Runs all calls concurrently for speed (~2-3s total).

Usage:
    python scripts/smoke_models.py              # from repo root
    python scripts/smoke_models.py --json       # machine-readable output
    python scripts/smoke_models.py --ci         # exit 1 on any failure (for CI)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config loading (standalone — no app imports needed)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "models.json"

PROVIDER_DEFAULT_KEYS = {
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def get_api_key(model_info: dict) -> tuple[str, str | None]:
    """Return (env_var_name, key_value) for a model."""
    env_var = model_info.get("apiKeyEnvVar") or PROVIDER_DEFAULT_KEYS.get(model_info["provider"], "")
    return env_var, os.getenv(env_var, "").strip() or None


# ---------------------------------------------------------------------------
# Smoke callers
# ---------------------------------------------------------------------------


async def smoke_text_openai(model_id: str, api_key: str, base_url: str | None) -> str:
    """Ping an OpenAI-compatible text model."""
    import re

    from openai import AsyncOpenAI

    kwargs: dict = {"api_key": api_key, "timeout": 15.0}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    try:
        resp = await client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
            max_completion_tokens=64,
        )
        msg = resp.choices[0].message
        content = msg.content or ""
        # Some reasoning models return output in a separate field
        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            content = content or msg.reasoning_content
        # Got a valid response with content or a finish_reason = model is alive
        if content or resp.choices[0].finish_reason:
            return "pass"
        return "empty response"
    finally:
        await client.close()


async def smoke_text_anthropic(model_id: str, api_key: str, base_url: str | None) -> str:
    """Ping an Anthropic-compatible text model."""
    from anthropic import AsyncAnthropic

    kwargs: dict = {"api_key": api_key, "timeout": 15.0}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncAnthropic(**kwargs)
    try:
        resp = await client.messages.create(
            model=model_id,
            max_tokens=32,
            messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
        )
        content = resp.content[0].text if resp.content else ""
        if len(content) > 0:
            return "pass"
        return "empty response"
    finally:
        await client.close()


async def smoke_embedding(model_id: str, api_key: str, dims: int) -> str:
    """Ping an embedding model."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, timeout=15.0)
    try:
        resp = await client.embeddings.create(model=model_id, input="test", dimensions=dims)
        if resp.data and len(resp.data[0].embedding) == dims:
            return "pass"
        return f"unexpected dims: {len(resp.data[0].embedding) if resp.data else 'no data'}"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def smoke_one_model(model_id: str, model_info: dict, category: str) -> dict:
    """Test a single model. Returns result dict."""
    env_var, api_key = get_api_key(model_info)
    if not api_key:
        return {"model": model_id, "category": category, "status": "skipped", "reason": f"{env_var} not set"}

    provider = model_info["provider"]
    base_url = model_info.get("baseUrl")
    t0 = time.monotonic()

    try:
        if category == "embedding":
            result = await smoke_embedding(model_id, api_key, model_info.get("dims", 256))
        elif provider == "anthropic":
            result = await smoke_text_anthropic(model_id, api_key, base_url)
        else:
            result = await smoke_text_openai(model_id, api_key, base_url)

        elapsed = round((time.monotonic() - t0) * 1000)
        status = "pass" if result == "pass" else "fail"
        return {"model": model_id, "category": category, "status": status, "ms": elapsed, "detail": result}

    except Exception as e:
        elapsed = round((time.monotonic() - t0) * 1000)
        return {"model": model_id, "category": category, "status": "fail", "ms": elapsed, "detail": str(e)}


async def run_all() -> list[dict]:
    config = load_config()
    tasks = []

    # Text models
    for model_id, model_info in config.get("text", {}).get("models", {}).items():
        tasks.append(smoke_one_model(model_id, model_info, "text"))

    # Realtime models — skip (require WebSocket, not HTTP)

    # Embedding models
    emb_default = config.get("embedding", {}).get("default")
    if emb_default:
        tasks.append(smoke_one_model(emb_default["model"], {**emb_default, "provider": emb_default["provider"]}, "embedding"))

    return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    json_mode = "--json" in sys.argv
    ci_mode = "--ci" in sys.argv

    results = asyncio.run(run_all())

    if json_mode:
        print(json.dumps(results, indent=2))
    else:
        passed = sum(1 for r in results if r["status"] == "pass")
        failed = sum(1 for r in results if r["status"] == "fail")
        skipped = sum(1 for r in results if r["status"] == "skipped")

        for r in results:
            icon = {"pass": "OK", "fail": "FAIL", "skipped": "SKIP"}[r["status"]]
            ms = f" ({r['ms']}ms)" if "ms" in r else ""
            detail = f" — {r.get('detail') or r.get('reason', '')}" if r["status"] != "pass" else ""
            print(f"  [{icon:>4}] {r['category']:>9}  {r['model']}{ms}{detail}")

        print(f"\n  {passed} passed, {failed} failed, {skipped} skipped")

    if ci_mode and any(r["status"] == "fail" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
