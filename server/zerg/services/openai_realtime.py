"""Compatibility wrapper for realtime voice helpers (moved to zerg.voice)."""

# NOTE: tests patch zerg.services.openai_realtime.httpx.AsyncClient, so keep httpx here.
import httpx  # noqa: F401

from zerg.voice.realtime import get_default_voice
from zerg.voice.realtime import get_realtime_model
from zerg.voice.realtime import mint_realtime_session_token

__all__ = [
    "get_default_voice",
    "get_realtime_model",
    "mint_realtime_session_token",
]
