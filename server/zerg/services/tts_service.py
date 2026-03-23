"""Compatibility wrapper for TTS service (moved to zerg.voice)."""

from zerg.voice.tts_service import TTSConfig
from zerg.voice.tts_service import TTSProvider
from zerg.voice.tts_service import TTSResult
from zerg.voice.tts_service import TTSService
from zerg.voice.tts_service import get_tts_service
from zerg.voice.tts_service import text_to_speech

__all__ = [
    "TTSConfig",
    "TTSProvider",
    "TTSResult",
    "TTSService",
    "get_tts_service",
    "text_to_speech",
]
