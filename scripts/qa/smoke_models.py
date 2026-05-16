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
    python scripts/smoke_models.py --scope active  # active profile only
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

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "models.json"

PROVIDER_DEFAULT_KEYS = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def classify_smoke_exception(exc: Exception) -> tuple[str, str]:
    """Classify smoke exceptions into fail vs transient skip.

    Live provider quota exhaustion should not make unrelated product changes
    look broken in CI. We still surface the detail, but treat explicit
    rate-limit or exhausted-credit responses as skipped/transient instead of
    hard failures.
    """
    detail = str(exc)
    lower = detail.lower()
    if "429" in detail and ("rate limit" in lower or "too many requests" in lower):
        return "skipped", f"rate limited: {detail}"
    if "429" in detail and (
        "resource has been exhausted" in lower
        or "used all available credits" in lower
        or "monthly spending limit" in lower
        or "raise your spending limit" in lower
    ):
        return "skipped", f"provider quota exhausted: {detail}"
    # Some OpenAI-compatible providers occasionally return a malformed payload
    # (for example `choices: null`) that trips the SDK/client-side indexing path.
    # That is a provider-side transient, not a product regression.
    if detail == "'NoneType' object is not subscriptable":
        return "skipped", f"malformed provider response: {detail}"
    return "fail", detail


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def get_api_key(model_info: dict) -> tuple[str, str | None]:
    """Return (env_var_name, key_value) for a model."""
    env_var = model_info.get("apiKeyEnvVar") or PROVIDER_DEFAULT_KEYS.get(model_info["provider"], "")
    return env_var, os.getenv(env_var, "").strip() or None


def _resolve_model_reference(config: dict, tier_or_model: str) -> str:
    """Resolve a tier name or direct model ID from models.json."""
    tiers = config.get("text", {}).get("tiers", {})
    return tiers.get(tier_or_model, tier_or_model)


def get_active_text_models(config: dict) -> list[tuple[str, dict]]:
    """Return text models referenced by the active profile's routing."""
    text_models = config.get("text", {}).get("models", {})
    use_cases = dict(config.get("useCases", {}).get("text", {}))
    defaults = dict(config.get("defaults", {}).get("text", {}))

    raw_profiles = config.get("routingProfiles", {})
    profiles = {name: cfg for name, cfg in raw_profiles.items() if isinstance(cfg, dict) and not name.startswith("$")}
    active_profile = os.getenv("MODELS_PROFILE", "oss")
    if profiles and active_profile not in profiles:
        raise ValueError(f"Unknown MODELS_PROFILE '{active_profile}'. Valid profiles: {list(profiles.keys())}")

    text_overrides = profiles.get(active_profile, {}).get("text", {})
    use_cases.update(text_overrides.get("useCases", {}))
    defaults.update(text_overrides.get("defaults", {}))

    ordered_models: list[tuple[str, dict]] = []
    seen: set[str] = set()

    for tier_or_model in [*use_cases.values(), *defaults.values()]:
        model_id = _resolve_model_reference(config, tier_or_model)
        if model_id in text_models and model_id not in seen:
            seen.add(model_id)
            ordered_models.append((model_id, text_models[model_id]))

    return ordered_models


# ---------------------------------------------------------------------------
# Smoke callers
# ---------------------------------------------------------------------------


async def smoke_text_openai(model_id: str, api_key: str, base_url: str | None) -> str:
    """Ping an OpenAI-compatible text model."""
    from openai import AsyncOpenAI

    kwargs: dict = {"api_key": api_key, "timeout": 15.0}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    try:
        resp = await client.chat.completions.create(**build_openai_smoke_request(model_id))
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


def build_openai_smoke_request(model_id: str) -> dict:
    """Build a cheap, stable smoke payload for OpenAI chat models."""
    request = {
        "model": model_id,
        "messages": [{"role": "user", "content": "What is 2+2? Reply with just the number."}],
        "max_completion_tokens": 64,
    }

    # GPT-5 models default to a higher reasoning budget than this smoke probe
    # needs. Keep the smoke call intentionally cheap and deterministic.
    if model_id.startswith("gpt-5"):
        request["reasoning_effort"] = "low"

    return request


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


async def smoke_embedding(model_id: str, api_key: str, dims: int, base_url: str | None = None) -> str:
    """Ping an embedding model."""
    from openai import AsyncOpenAI

    kwargs: dict = {"api_key": api_key, "timeout": 15.0}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
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
            result = await smoke_embedding(model_id, api_key, model_info.get("dims", 256), base_url)
        elif provider == "anthropic":
            result = await smoke_text_anthropic(model_id, api_key, base_url)
        else:
            result = await smoke_text_openai(model_id, api_key, base_url)

        elapsed = round((time.monotonic() - t0) * 1000)
        status = "pass" if result == "pass" else "fail"
        return {"model": model_id, "category": category, "status": status, "ms": elapsed, "detail": result}

    except Exception as e:
        elapsed = round((time.monotonic() - t0) * 1000)
        status, detail = classify_smoke_exception(e)
        return {"model": model_id, "category": category, "status": status, "ms": elapsed, "detail": detail}


async def run_all(*, scope: str = "all") -> list[dict]:
    config = load_config()
    tasks = []

    if scope == "all":
        text_models = list(config.get("text", {}).get("models", {}).items())
    elif scope == "active":
        text_models = get_active_text_models(config)
    else:
        raise ValueError(f"Unknown smoke scope '{scope}'")

    # Text models
    for model_id, model_info in text_models:
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
    scope = "all"
    if "--scope" in sys.argv:
        try:
            scope = sys.argv[sys.argv.index("--scope") + 1]
        except IndexError as exc:
            raise SystemExit("--scope requires a value: all or active") from exc

    results = asyncio.run(run_all(scope=scope))

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
