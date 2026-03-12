"""Smoke test: verify every model in config/models.json responds to a trivial call.

Skips entirely when no LLM API keys are set (normal for CI unit tests).
Run with real keys to validate model availability:
    OPENAI_API_KEY=... GROQ_API_KEY=... make test
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# Add scripts dir so we can import the smoke runner
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts"))

from smoke_models import load_config, get_api_key, run_all  # noqa: E402

_HAS_ANY_KEY = bool(
    os.getenv("OPENAI_API_KEY", "").strip()
    or os.getenv("GROQ_API_KEY", "").strip()
    or os.getenv("XAI_API_KEY", "").strip()
    or os.getenv("ZAI_API_KEY", "").strip()
)


@pytest.mark.skipif(not _HAS_ANY_KEY, reason="No LLM API keys set — skipping model smoke test")
def test_model_smoke_all_available_models_respond():
    """Every model whose API key is available must return a non-error response."""
    results = asyncio.run(run_all())

    failures = [r for r in results if r["status"] == "fail"]
    skipped = [r for r in results if r["status"] == "skipped"]
    passed = [r for r in results if r["status"] == "pass"]

    # Print summary for test output
    for r in results:
        icon = {"pass": "OK", "fail": "FAIL", "skipped": "SKIP"}[r["status"]]
        print(f"  [{icon:>4}] {r['category']:>9}  {r['model']}  {r.get('detail', r.get('reason', ''))}")

    assert not failures, (
        f"{len(failures)} model(s) failed:\n"
        + "\n".join(f"  - {r['model']}: {r['detail']}" for r in failures)
    )


def test_models_json_is_valid_and_all_models_have_api_key_config():
    """Structural check: every model in models.json has a resolvable API key env var."""
    config = load_config()

    for model_id, model_info in config.get("text", {}).get("models", {}).items():
        env_var, _ = get_api_key(model_info)
        assert env_var, f"Model {model_id} has no API key env var configured"

    emb = config.get("embedding", {}).get("default")
    if emb:
        env_var, _ = get_api_key(emb)
        assert env_var, "Embedding model has no API key env var configured"
