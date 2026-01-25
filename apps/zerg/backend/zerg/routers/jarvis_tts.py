"""Jarvis TTS Router - Text-to-Speech endpoints for voice responses.

Provides:
- POST /api/jarvis/tts - Convert text to speech
- GET /api/jarvis/tts/stream - Stream audio generation
- GET /api/jarvis/tts/status - Get TTS configuration status
- GET /api/jarvis/tts/voices - List available voices
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field

from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.services.tts_service import TTSProvider
from zerg.services.tts_service import get_tts_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jarvis-tts"])


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class TTSRequest(BaseModel):
    """Request to convert text to speech."""

    text: str = Field(..., description="Text to convert to speech", min_length=1, max_length=4000)
    provider: Optional[str] = Field(None, description="TTS provider: elevenlabs or edge (default: auto)")
    voice_id: Optional[str] = Field(None, description="ElevenLabs voice ID (if using elevenlabs)")


class TTSResponse(BaseModel):
    """Response from TTS conversion (metadata only)."""

    success: bool = Field(..., description="Whether conversion succeeded")
    latency_ms: Optional[int] = Field(None, description="Conversion latency in milliseconds")
    provider: Optional[str] = Field(None, description="Provider used for conversion")
    output_format: Optional[str] = Field(None, description="Audio output format")
    content_type: Optional[str] = Field(None, description="Audio MIME type")
    error: Optional[str] = Field(None, description="Error message if failed")


class TTSStatusResponse(BaseModel):
    """TTS service status."""

    enabled: bool = Field(..., description="Whether TTS is enabled")
    default_provider: str = Field(..., description="Default TTS provider")
    available_providers: list[str] = Field(..., description="List of available providers")
    max_text_length: int = Field(..., description="Maximum text length for TTS")


class VoiceInfo(BaseModel):
    """Information about a TTS voice."""

    id: str = Field(..., description="Voice identifier")
    name: str = Field(..., description="Human-readable voice name")
    provider: str = Field(..., description="Provider this voice belongs to")
    language: str = Field(..., description="Language code")
    gender: Optional[str] = Field(None, description="Voice gender (male/female/neutral)")


class VoicesResponse(BaseModel):
    """List of available voices."""

    voices: list[VoiceInfo] = Field(..., description="Available voices")


# ---------------------------------------------------------------------------
# Edge TTS Voice List (commonly used voices)
# ---------------------------------------------------------------------------

EDGE_VOICES = [
    VoiceInfo(id="en-US-GuyNeural", name="Guy (US)", provider="edge", language="en-US", gender="male"),
    VoiceInfo(id="en-US-JennyNeural", name="Jenny (US)", provider="edge", language="en-US", gender="female"),
    VoiceInfo(id="en-US-AriaNeural", name="Aria (US)", provider="edge", language="en-US", gender="female"),
    VoiceInfo(id="en-US-DavisNeural", name="Davis (US)", provider="edge", language="en-US", gender="male"),
    VoiceInfo(id="en-US-TonyNeural", name="Tony (US)", provider="edge", language="en-US", gender="male"),
    VoiceInfo(id="en-GB-RyanNeural", name="Ryan (UK)", provider="edge", language="en-GB", gender="male"),
    VoiceInfo(id="en-GB-SoniaNeural", name="Sonia (UK)", provider="edge", language="en-GB", gender="female"),
    VoiceInfo(id="en-AU-WilliamNeural", name="William (AU)", provider="edge", language="en-AU", gender="male"),
    VoiceInfo(id="en-AU-NatashaNeural", name="Natasha (AU)", provider="edge", language="en-AU", gender="female"),
]

ELEVENLABS_DEFAULT_VOICES = [
    VoiceInfo(id="pMsXgVXv3BLzUgSXRplE", name="Adam", provider="elevenlabs", language="en", gender="male"),
    VoiceInfo(id="21m00Tcm4TlvDq8ikWAM", name="Rachel", provider="elevenlabs", language="en", gender="female"),
    VoiceInfo(id="AZnzlk1XvdvUeBnXmlld", name="Domi", provider="elevenlabs", language="en", gender="female"),
    VoiceInfo(id="EXAVITQu4vr4xnSDxMaL", name="Bella", provider="elevenlabs", language="en", gender="female"),
    VoiceInfo(id="ErXwobaYiN019PkySvjV", name="Antoni", provider="elevenlabs", language="en", gender="male"),
    VoiceInfo(id="MF3mGyEYCl7XYWbV9V6O", name="Elli", provider="elevenlabs", language="en", gender="female"),
    VoiceInfo(id="TxGEqnHWrfWFTfGW9XjX", name="Josh", provider="elevenlabs", language="en", gender="male"),
    VoiceInfo(id="VR6AewLTigWG4xSOukaG", name="Arnold", provider="elevenlabs", language="en", gender="male"),
]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/tts", response_class=Response)
async def convert_text_to_speech(
    request: TTSRequest,
    current_user=Depends(get_current_jarvis_user),
) -> Response:
    """Convert text to speech and return audio.

    Returns audio data directly in the response body.
    Content-Type will be audio/mpeg for MP3 format.
    """
    tts_service = get_tts_service()

    # Parse provider if specified
    provider = None
    if request.provider:
        try:
            provider = TTSProvider(request.provider.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid provider: {request.provider}. Must be one of: {[p.value for p in TTSProvider]}",
            )

    result = await tts_service.convert(request.text, provider, request.voice_id)

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.error or "TTS conversion failed",
        )

    return Response(
        content=result.audio_data,
        media_type=result.content_type,
        headers={
            "X-TTS-Provider": result.provider or "unknown",
            "X-TTS-Latency-Ms": str(result.latency_ms or 0),
            "X-TTS-Format": result.output_format or "mp3",
        },
    )


@router.post("/tts/json", response_model=TTSResponse)
async def convert_text_to_speech_json(
    request: TTSRequest,
    current_user=Depends(get_current_jarvis_user),
) -> TTSResponse:
    """Convert text to speech and return metadata (without audio).

    Use this endpoint to check if TTS would succeed without actually
    generating the audio. Useful for validation.
    """
    tts_service = get_tts_service()

    provider = None
    if request.provider:
        try:
            provider = TTSProvider(request.provider.lower())
        except ValueError:
            return TTSResponse(
                success=False,
                error=f"Invalid provider: {request.provider}",
            )

    result = await tts_service.convert(request.text, provider, request.voice_id)

    return TTSResponse(
        success=result.success,
        latency_ms=result.latency_ms,
        provider=result.provider,
        output_format=result.output_format,
        content_type=result.content_type,
        error=result.error,
    )


@router.get("/tts/stream")
async def stream_text_to_speech(
    text: str,
    provider: Optional[str] = None,
    current_user=Depends(get_current_jarvis_user),
) -> StreamingResponse:
    """Stream audio generation.

    For now, this streams the complete audio once generated.
    Future: implement true streaming with ElevenLabs.
    """
    if not text or not text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Text parameter is required",
        )

    tts_service = get_tts_service()

    # Parse provider
    tts_provider = None
    if provider:
        try:
            tts_provider = TTSProvider(provider.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid provider: {provider}",
            )

    async def generate():
        async for chunk in tts_service.stream_audio(text, tts_provider):
            yield chunk

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "Transfer-Encoding": "chunked",
        },
    )


@router.get("/tts/status", response_model=TTSStatusResponse)
async def get_tts_status(
    current_user=Depends(get_current_jarvis_user),
) -> TTSStatusResponse:
    """Get TTS service status and configuration."""
    tts_service = get_tts_service()
    config = tts_service.config

    # Determine available providers
    available = []
    for provider in TTSProvider:
        if tts_service._is_provider_available(provider):
            available.append(provider.value)

    return TTSStatusResponse(
        enabled=config.enabled,
        default_provider=config.provider.value,
        available_providers=available,
        max_text_length=config.max_text_length,
    )


@router.get("/tts/voices", response_model=VoicesResponse)
async def list_voices(
    provider: Optional[str] = None,
    current_user=Depends(get_current_jarvis_user),
) -> VoicesResponse:
    """List available TTS voices.

    Args:
        provider: Filter by provider (elevenlabs, edge, or all)

    Returns:
        List of available voices with metadata
    """
    tts_service = get_tts_service()

    voices = []

    # Add Edge voices if available
    if provider is None or provider.lower() == "edge":
        voices.extend(EDGE_VOICES)

    # Add ElevenLabs voices if available
    if provider is None or provider.lower() == "elevenlabs":
        if tts_service._is_provider_available(TTSProvider.ELEVENLABS):
            voices.extend(ELEVENLABS_DEFAULT_VOICES)

    return VoicesResponse(voices=voices)
