"""Helpers for audit metadata on OpenAI-compatible audio requests."""

from __future__ import annotations

import os
from urllib.parse import urlparse

_PROXY_HOSTS_REQUIRING_SOURCE = {"litellm-proxy", "llm.drose.io"}


def get_openai_audio_extra_body(default_source: str, *, api_key: str | None = None) -> dict[str, dict[str, str]] | None:
    """Return extra request metadata when the configured endpoint requires source tags."""
    override = (os.getenv("OPENAI_METADATA_SOURCE") or "").strip()
    if override:
        return {"metadata": {"source": override}}

    base_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
    if base_url:
        host = (urlparse(base_url).hostname or "").strip().lower()
        if host in _PROXY_HOSTS_REQUIRING_SOURCE:
            return {"metadata": {"source": default_source}}

    if api_key and api_key.startswith("sk-litellm-"):
        return {"metadata": {"source": default_source}}

    return None
