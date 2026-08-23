"""Smoke test: verify every model in config/models.json responds to a trivial call.

Skipped unless explicitly opted into. To validate model availability:
    LONGHOUSE_MODEL_SMOKE=1 OPENAI_API_KEY=... GROQ_API_KEY=... make test

Opt-in on purpose: `get_settings()` loads `.env`, so provider keys are present
during an ordinary unit run and key presence alone would bill live calls.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# Add scripts dir so we can import the smoke runner
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "qa"))

from smoke_models import get_api_key  # noqa: E402
from smoke_models import load_config  # noqa: E402
from smoke_models import run_all  # noqa: E402

_KEY_VARIABLES = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "ZAI_API_KEY",
)


_OPT_IN = "LONGHOUSE_MODEL_SMOKE"


def _opted_in() -> bool:
    """Has someone explicitly asked for live provider calls?

    Key presence is not consent. `get_settings()` loads the developer's `.env`,
    so by the time anything in the suite has touched settings, real OpenAI /
    OpenRouter / Groq keys are in `os.environ` — and this test then billed live
    calls as part of `make test`. It also read as flaky, because running the file
    alone never loaded settings and so always skipped.

    Both reads happen at call time, not import time: as a module-level constant
    the guard's value depended on which tests imported first.
    """

    if not os.getenv(_OPT_IN, "").strip():
        return False
    return any(os.getenv(name, "").strip() for name in _KEY_VARIABLES)


@pytest.mark.timeout(30)
def test_model_smoke_active_profile_models_respond():
    """Every model referenced by the active profile must return a non-error response."""
    if not _opted_in():
        pytest.skip(f"Live model smoke is opt-in; set {_OPT_IN}=1 with provider keys")
    results = asyncio.run(run_all(scope="active"))

    failures = [r for r in results if r["status"] == "fail"]
    # Print summary for test output
    for r in results:
        icon = {"pass": "OK", "fail": "FAIL", "skipped": "SKIP"}[r["status"]]
        print(f"  [{icon:>4}] {r['category']:>9}  {r['model']}  {r.get('detail', r.get('reason', ''))}")

    assert not failures, (
        f"{len(failures)} model(s) failed:\n"
        + "\n".join(f"  - {r['model']}: {r['detail']}" for r in failures)
    )


def test_models_json_is_valid_and_remote_models_have_api_key_config():
    """Remote text models need keys; the pinned local embedding model does not."""
    config = load_config()

    for model_id, model_info in config.get("text", {}).get("models", {}).items():
        env_var, _ = get_api_key(model_info)
        assert env_var, f"Model {model_id} has no API key env var configured"

    emb = config.get("embedding", {}).get("default")
    if emb:
        assert emb["provider"] == "local-onnx"
        assert "apiKeyEnvVar" not in emb
