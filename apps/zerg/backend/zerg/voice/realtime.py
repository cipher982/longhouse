"""OpenAI Realtime API client for session token minting.

This module provides direct access to OpenAI's Realtime API for minting
ephemeral session tokens used by the Jarvis voice interface.

Replaces the jarvis-server proxy layer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from zerg.config import get_settings


def _get_models_config() -> dict[str, Any]:
    """Load model configuration from config/models.json."""
    # Find the config file relative to repo root
    current_path = Path(__file__).resolve()
    if "/app/" in str(current_path):
        # Docker environment
        config_path = Path("/app/config/models.json")
    else:
        # Local monorepo: services/ is deep in apps/zerg/backend/zerg/
        repo_root = current_path.parents[5]
        config_path = repo_root / "config" / "models.json"

    if not config_path.exists():
        # Fallback defaults if config not found
        return {
            "realtime": {
                "tiers": {"TIER_1": "gpt-4o-realtime-preview"},
                "defaultVoice": "verse",
            }
        }

    with open(config_path) as f:
        return json.load(f)


def get_realtime_model() -> str:
    """Get the configured realtime model.

    Can be overridden via JARVIS_REALTIME_MODEL env var.
    """
    override = os.getenv("JARVIS_REALTIME_MODEL")
    if override:
        return override

    # Check for mini model preference
    if os.getenv("JARVIS_USE_MINI_MODEL") in ("1", "true", "yes"):
        config = _get_models_config()
        return config.get("realtime", {}).get("tiers", {}).get("TIER_2", "gpt-4o-mini-realtime-preview")

    config = _get_models_config()
    return config.get("realtime", {}).get("tiers", {}).get("TIER_1", "gpt-4o-realtime-preview")


def get_default_voice() -> str:
    """Get the configured default voice.

    Can be overridden via JARVIS_VOICE env var.
    """
    override = os.getenv("JARVIS_VOICE")
    if override:
        return override

    config = _get_models_config()
    return config.get("realtime", {}).get("defaultVoice", "verse")


async def mint_realtime_session_token() -> dict[str, Any]:
    """Mint an ephemeral OpenAI Realtime session token.

    Returns the raw response from OpenAI's client_secrets endpoint,
    which contains the ephemeral token for WebRTC connection.

    Raises:
        httpx.TimeoutException: If OpenAI API times out
        httpx.HTTPStatusError: If OpenAI API returns an error status
    """
    settings = get_settings()

    model = get_realtime_model()
    voice = get_default_voice()

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "session": {
                    "type": "realtime",
                    "model": model,
                    "audio": {"output": {"voice": voice}},
                }
            },
        )
        response.raise_for_status()
        return response.json()
