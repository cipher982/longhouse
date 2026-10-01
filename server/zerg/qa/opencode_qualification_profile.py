"""Build the disposable OpenCode profile used by live qualification producers.

The factory deliberately pins a newly released OpenRouter model. OpenCode only
accepts a configured model as the TUI default when that model is present in its
provider catalogue. A model can exist on OpenRouter before the models.dev
snapshot bundled with the staged OpenCode release knows about it; in that case
OpenCode silently falls back to the first known model. Register the exact model
in the disposable profile so the release probe exercises the configured model,
not whichever unrelated fallback happens to sort first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from zerg.qa.openrouter_routing import openrouter_qualification_routing

QUALIFICATION_MODEL_ENV = "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL"
RUNTIME_MODEL_ENV = "LONGHOUSE_OPENCODE_MODEL"


def configured_openrouter_model(environment: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(full_model, OpenRouter_model_id)`` for the explicit pin."""

    configured = str(environment.get(QUALIFICATION_MODEL_ENV) or "").strip()
    if not configured:
        raise RuntimeError("OpenCode live qualification requires an explicit model")
    full_model = configured if configured.startswith("openrouter/") else f"openrouter/{configured}"
    provider_id, separator, model_id = full_model.partition("/")
    if provider_id != "openrouter" or not separator or not model_id.strip():
        raise RuntimeError("OpenCode qualification model must use the OpenRouter provider")
    return full_model, model_id


def prepare_opencode_qualification_profile(home: Path, environment: dict[str, str]) -> dict[str, str]:
    """Register the exact qualification model in an isolated OpenCode home.

    The profile contains no credential. OpenCode continues to read the
    OpenRouter key from the process environment, while both the server bridge
    and attached TUI resolve one explicit, catalogue-valid model identity. The
    model's requests carry the qualification routing (``openrouter_routing``),
    so one slow OpenRouter host cannot stall a cell.
    """

    full_model, model_id = configured_openrouter_model(environment)
    routing = openrouter_qualification_routing()
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = config_dir / "opencode.json"
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": ["openrouter"],
        "model": full_model,
        # OpenCode otherwise chooses a separate catalogue default for title
        # work. Keep every model-backed action in this disposable probe on the
        # same declared credential/model authority.
        "small_model": full_model,
        # OpenCode sends a model's ``options.provider`` as the request's OpenRouter
        # routing object (verified against 1.18.30 with a local capture server).
        "provider": {"openrouter": {"models": {model_id: {"options": {"provider": routing}}}}},
    }
    temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    environment[RUNTIME_MODEL_ENV] = full_model
    return {
        "provider": "openrouter",
        "model": full_model,
        "model_id": model_id,
        "config_path": str(config_path),
        "selection_authority": "disposable_profile_and_runtime_override",
        "credential_authority": "process_environment:OPENROUTER_API_KEY",
        "routing": routing,
    }


__all__ = [
    "QUALIFICATION_MODEL_ENV",
    "RUNTIME_MODEL_ENV",
    "configured_openrouter_model",
    "prepare_opencode_qualification_profile",
]
