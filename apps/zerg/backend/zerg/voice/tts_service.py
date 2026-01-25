"""Text-to-Speech service for Jarvis voice responses.

Provides TTS conversion using multiple providers:
- ElevenLabs (premium, requires API key)
- Edge TTS (free fallback, uses Microsoft Edge's neural TTS)

Architecture follows the clawdbot reference implementation with provider fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class TTSProvider(str, Enum):
    """Available TTS providers."""

    ELEVENLABS = "elevenlabs"
    EDGE = "edge"


@dataclass
class TTSConfig:
    """TTS configuration settings."""

    enabled: bool = True
    provider: TTSProvider = TTSProvider.EDGE
    max_text_length: int = 4000
    timeout_ms: int = 30000

    # ElevenLabs settings
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str = "pMsXgVXv3BLzUgSXRplE"  # Default: Adam
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_stability: float = 0.5
    elevenlabs_similarity_boost: float = 0.75
    elevenlabs_style: float = 0.0
    elevenlabs_use_speaker_boost: bool = True
    elevenlabs_speed: float = 1.0

    # Edge TTS settings
    edge_voice: str = "en-US-GuyNeural"  # Default: masculine voice for Jarvis
    edge_lang: str = "en-US"
    edge_rate: str | None = None  # e.g., "+10%", "-5%", None for default
    edge_pitch: str | None = None  # e.g., "+10Hz", "-5Hz", None for default
    edge_volume: str | None = None  # e.g., "+10%", "-5%", None for default

    @classmethod
    def from_env(cls) -> "TTSConfig":
        """Load configuration from environment variables."""

        def _truthy(val: str | None) -> bool:
            return val is not None and val.strip().lower() in {"1", "true", "yes", "on"}

        provider_str = os.getenv("TTS_PROVIDER", "edge").lower()
        provider = TTSProvider(provider_str) if provider_str in [p.value for p in TTSProvider] else TTSProvider.EDGE

        return cls(
            enabled=_truthy(os.getenv("TTS_ENABLED", "1")),
            provider=provider,
            max_text_length=int(os.getenv("TTS_MAX_TEXT_LENGTH", "4000")),
            timeout_ms=int(os.getenv("TTS_TIMEOUT_MS", "30000")),
            # ElevenLabs
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY") or os.getenv("XI_API_KEY"),
            elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "pMsXgVXv3BLzUgSXRplE"),
            elevenlabs_model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
            elevenlabs_stability=float(os.getenv("ELEVENLABS_STABILITY", "0.5")),
            elevenlabs_similarity_boost=float(os.getenv("ELEVENLABS_SIMILARITY_BOOST", "0.75")),
            elevenlabs_style=float(os.getenv("ELEVENLABS_STYLE", "0.0")),
            elevenlabs_use_speaker_boost=_truthy(os.getenv("ELEVENLABS_USE_SPEAKER_BOOST", "1")),
            elevenlabs_speed=float(os.getenv("ELEVENLABS_SPEED", "1.0")),
            # Edge TTS
            edge_voice=os.getenv("TTS_EDGE_VOICE", "en-US-GuyNeural"),
            edge_lang=os.getenv("TTS_EDGE_LANG", "en-US"),
            edge_rate=os.getenv("TTS_EDGE_RATE") or None,
            edge_pitch=os.getenv("TTS_EDGE_PITCH") or None,
            edge_volume=os.getenv("TTS_EDGE_VOLUME") or None,
        )


@dataclass
class TTSResult:
    """Result of TTS conversion."""

    success: bool
    audio_path: str | None = None
    audio_data: bytes | None = None
    error: str | None = None
    latency_ms: int | None = None
    provider: str | None = None
    output_format: str | None = None
    content_type: str = "audio/mpeg"


class TTSService:
    """Text-to-Speech service with provider fallback."""

    def __init__(self, config: TTSConfig | None = None):
        self.config = config or TTSConfig.from_env()
        self._temp_dir: Path | None = None

    def _get_temp_dir(self) -> Path:
        """Get or create temporary directory for audio files."""
        if self._temp_dir is None or not self._temp_dir.exists():
            self._temp_dir = Path(tempfile.mkdtemp(prefix="tts-"))
        return self._temp_dir

    def _get_provider_order(self) -> list[TTSProvider]:
        """Get provider order based on configuration and availability."""
        primary = self.config.provider
        providers = [primary]

        # Add fallbacks
        for p in TTSProvider:
            if p != primary:
                providers.append(p)

        return providers

    def _is_provider_available(self, provider: TTSProvider) -> bool:
        """Check if a provider is available (has required credentials)."""
        if provider == TTSProvider.ELEVENLABS:
            return bool(self.config.elevenlabs_api_key)
        if provider == TTSProvider.EDGE:
            return True  # Edge TTS requires no credentials
        return False

    async def _convert_elevenlabs(self, text: str, voice_id: str | None = None) -> TTSResult:
        """Convert text to speech using ElevenLabs API."""
        if not self.config.elevenlabs_api_key:
            return TTSResult(success=False, error="ElevenLabs API key not configured")

        import time

        start_time = time.time()
        # Use provided voice_id or fall back to config
        effective_voice_id = voice_id or self.config.elevenlabs_voice_id

        try:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{effective_voice_id}"

            async with httpx.AsyncClient(timeout=self.config.timeout_ms / 1000) as client:
                response = await client.post(
                    url,
                    headers={
                        "xi-api-key": self.config.elevenlabs_api_key,
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg",
                    },
                    json={
                        "text": text,
                        "model_id": self.config.elevenlabs_model_id,
                        "voice_settings": {
                            "stability": self.config.elevenlabs_stability,
                            "similarity_boost": self.config.elevenlabs_similarity_boost,
                            "style": self.config.elevenlabs_style,
                            "use_speaker_boost": self.config.elevenlabs_use_speaker_boost,
                            "speed": self.config.elevenlabs_speed,
                        },
                    },
                )

                if response.status_code != 200:
                    error_text = response.text[:200] if response.text else "Unknown error"
                    return TTSResult(
                        success=False,
                        error=f"ElevenLabs API error ({response.status_code}): {error_text}",
                    )

                audio_data = response.content
                latency_ms = int((time.time() - start_time) * 1000)

                # Save to temp file
                temp_dir = self._get_temp_dir()
                audio_path = temp_dir / f"tts-{int(time.time() * 1000)}.mp3"
                audio_path.write_bytes(audio_data)

                return TTSResult(
                    success=True,
                    audio_path=str(audio_path),
                    audio_data=audio_data,
                    latency_ms=latency_ms,
                    provider="elevenlabs",
                    output_format="mp3",
                    content_type="audio/mpeg",
                )

        except httpx.TimeoutException:
            return TTSResult(success=False, error="ElevenLabs request timed out")
        except Exception as e:
            return TTSResult(success=False, error=f"ElevenLabs error: {str(e)}")

    async def _convert_edge(self, text: str, voice_id: str | None = None) -> TTSResult:
        """Convert text to speech using Edge TTS (Microsoft's free neural TTS)."""
        import time

        start_time = time.time()
        # Use provided voice_id or fall back to config
        effective_voice = voice_id or self.config.edge_voice

        try:
            # Import edge-tts (optional dependency)
            try:
                import edge_tts
            except ImportError:
                return TTSResult(success=False, error="edge-tts package not installed. Run: uv add edge-tts")

            # Create communicate object with optional parameters
            kwargs = {"voice": effective_voice}
            if self.config.edge_rate:
                kwargs["rate"] = self.config.edge_rate
            if self.config.edge_pitch:
                kwargs["pitch"] = self.config.edge_pitch
            if self.config.edge_volume:
                kwargs["volume"] = self.config.edge_volume

            communicate = edge_tts.Communicate(text, **kwargs)

            # Generate audio to temp file
            temp_dir = self._get_temp_dir()
            audio_path = temp_dir / f"tts-{int(time.time() * 1000)}.mp3"

            await communicate.save(str(audio_path))

            # Read the audio data
            audio_data = audio_path.read_bytes()
            latency_ms = int((time.time() - start_time) * 1000)

            return TTSResult(
                success=True,
                audio_path=str(audio_path),
                audio_data=audio_data,
                latency_ms=latency_ms,
                provider="edge",
                output_format="mp3",
                content_type="audio/mpeg",
            )

        except asyncio.TimeoutError:
            return TTSResult(success=False, error="Edge TTS request timed out")
        except Exception as e:
            return TTSResult(success=False, error=f"Edge TTS error: {str(e)}")

    async def convert(
        self,
        text: str,
        provider: TTSProvider | None = None,
        voice_id: str | None = None,
    ) -> TTSResult:
        """Convert text to speech.

        Args:
            text: Text to convert to speech
            provider: Optional specific provider to use (overrides config)
            voice_id: Optional voice ID override (for ElevenLabs, or Edge voice name)

        Returns:
            TTSResult with audio data or error information
        """
        if not self.config.enabled:
            return TTSResult(success=False, error="TTS is disabled")

        if not text or not text.strip():
            return TTSResult(success=False, error="Empty text provided")

        # Enforce text length limit
        if len(text) > self.config.max_text_length:
            return TTSResult(
                success=False,
                error=f"Text too long ({len(text)} chars, max {self.config.max_text_length})",
            )

        # Get provider order
        if provider:
            providers = [provider]
        else:
            providers = self._get_provider_order()

        last_error: str | None = None

        for p in providers:
            if not self._is_provider_available(p):
                last_error = f"{p.value}: not available (missing credentials)"
                logger.debug(f"TTS: skipping {p.value} - not available")
                continue

            logger.debug(f"TTS: trying provider {p.value}")

            if p == TTSProvider.ELEVENLABS:
                result = await self._convert_elevenlabs(text, voice_id)
            elif p == TTSProvider.EDGE:
                result = await self._convert_edge(text, voice_id)
            else:
                continue

            if result.success:
                logger.info(f"TTS: converted {len(text)} chars in {result.latency_ms}ms using {p.value}")
                return result

            last_error = result.error
            logger.warning(f"TTS: {p.value} failed: {result.error}")

        return TTSResult(
            success=False,
            error=f"All TTS providers failed. Last error: {last_error or 'unknown'}",
        )

    async def stream_audio(
        self,
        text: str,
        provider: TTSProvider | None = None,
    ):
        """Stream audio generation (yields chunks as they become available).

        For Edge TTS, uses true streaming via communicate.stream().
        For ElevenLabs, yields the complete audio in one chunk (streaming API not implemented).
        """
        if not self.config.enabled:
            raise RuntimeError("TTS is disabled")

        if not text or not text.strip():
            raise RuntimeError("Empty text provided")

        if len(text) > self.config.max_text_length:
            raise RuntimeError(f"Text too long ({len(text)} chars, max {self.config.max_text_length})")

        # Select provider
        selected_provider = provider or self.config.provider

        # Try Edge TTS with true streaming
        if selected_provider == TTSProvider.EDGE and self._is_provider_available(TTSProvider.EDGE):
            try:
                import edge_tts

                kwargs = {"voice": self.config.edge_voice}
                if self.config.edge_rate:
                    kwargs["rate"] = self.config.edge_rate
                if self.config.edge_pitch:
                    kwargs["pitch"] = self.config.edge_pitch
                if self.config.edge_volume:
                    kwargs["volume"] = self.config.edge_volume

                communicate = edge_tts.Communicate(text, **kwargs)

                # True streaming: yield chunks as they arrive
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        yield chunk["data"]

                return
            except Exception as e:
                logger.warning(f"TTS stream: Edge failed: {e}")
                # Fall through to non-streaming conversion

        # Fall back to non-streaming conversion
        result = await self.convert(text, provider)

        if result.success and result.audio_data:
            yield result.audio_data
        else:
            raise RuntimeError(result.error or "TTS conversion failed")

    def cleanup(self) -> None:
        """Clean up temporary files."""
        if self._temp_dir and self._temp_dir.exists():
            import shutil

            try:
                shutil.rmtree(self._temp_dir)
                self._temp_dir = None
            except Exception as e:
                logger.warning(f"TTS: failed to cleanup temp dir: {e}")


# Singleton instance
_tts_service: TTSService | None = None


def get_tts_service() -> TTSService:
    """Get the singleton TTS service instance."""
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service


# Convenience functions
async def text_to_speech(
    text: str,
    provider: TTSProvider | None = None,
    voice_id: str | None = None,
) -> TTSResult:
    """Convert text to speech using the default service."""
    return await get_tts_service().convert(text, provider, voice_id)
