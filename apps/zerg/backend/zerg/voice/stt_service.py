"""Speech-to-text (STT) service using OpenAI Audio Transcriptions."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from zerg.config import get_settings

logger = logging.getLogger(__name__)


# Normalize content-type values like "audio/webm;codecs=opus" -> "audio/webm"
def normalize_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()


# OpenAI-supported audio types for transcription
ALLOWED_AUDIO_TYPES = {
    "audio/flac",
    "audio/mp3",
    "audio/mp4",
    "audio/mpeg",
    "audio/mpga",
    "audio/m4a",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
}

# OpenAI's published per-request size limit for audio transcriptions
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25MB

DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
SUPPORTED_STT_MODELS = {
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe-diarize",
    "whisper-1",
}


@dataclass
class STTResult:
    """Result from an STT transcription call."""

    success: bool
    text: str | None = None
    error: str | None = None
    model: str | None = None


class STTService:
    """OpenAI-only speech-to-text service."""

    def __init__(self, model: str = DEFAULT_STT_MODEL, client: AsyncOpenAI | None = None) -> None:
        self._model = model
        self._client = client

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            settings = get_settings()
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    async def transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        prompt: str | None = None,
        language: str | None = None,
        model: str | None = None,
    ) -> STTResult:
        """Transcribe raw audio bytes.

        Args:
            audio_bytes: Raw audio file bytes
            filename: Optional filename (for MIME inference)
            content_type: Optional content type (e.g., audio/wav)
            prompt: Optional prompt for transcription biasing
            language: Optional ISO-639-1 language hint
            model: Optional override model
        """
        settings = get_settings()

        if settings.testing or settings.llm_disabled:
            logger.debug("STT: skipping external transcription in testing/disabled mode")
            return STTResult(success=True, text="Test transcript", model=model or self._model)

        if not audio_bytes:
            return STTResult(success=False, error="Empty audio payload")

        if len(audio_bytes) > MAX_AUDIO_BYTES:
            return STTResult(success=False, error=f"Audio file too large (max {MAX_AUDIO_BYTES // (1024 * 1024)}MB)")

        normalized_content_type = normalize_content_type(content_type)
        if normalized_content_type and normalized_content_type not in ALLOWED_AUDIO_TYPES:
            return STTResult(success=False, error=f"Unsupported audio type: {content_type}")

        selected_model = model or self._model
        if selected_model not in SUPPORTED_STT_MODELS:
            return STTResult(success=False, error=f"Unsupported STT model: {selected_model}")

        if not settings.openai_api_key:
            return STTResult(success=False, error="OpenAI API key not configured")

        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = filename or "audio.wav"

        try:
            response = await self._get_client().audio.transcriptions.create(
                model=selected_model,
                file=file_obj,
                response_format="json",
                prompt=prompt,
                language=language,
            )
        except Exception as exc:  # noqa: BLE001 - surface OpenAI errors to caller
            logger.exception("STT: transcription failed")
            return STTResult(success=False, error=str(exc), model=selected_model)

        text = getattr(response, "text", None)
        if text is None and isinstance(response, dict):
            text = response.get("text")

        if not text:
            return STTResult(success=False, error="Empty transcription result", model=selected_model)

        return STTResult(success=True, text=text, model=selected_model)


_stt_service: STTService | None = None


def get_stt_service() -> STTService:
    """Get singleton STT service instance."""
    global _stt_service
    if _stt_service is None:
        _stt_service = STTService()
    return _stt_service
