"""Jarvis turn-based voice endpoints."""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from pydantic import BaseModel

from zerg.config import get_settings
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.voice.stt_service import ALLOWED_AUDIO_TYPES
from zerg.voice.stt_service import MAX_AUDIO_BYTES
from zerg.voice.stt_service import normalize_content_type
from zerg.voice.tts_service import TTSProvider
from zerg.voice.tts_service import get_tts_service
from zerg.voice.turn_based import run_voice_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["jarvis-voice"])


class VoiceAudioResponse(BaseModel):
    """Optional audio payload for TTS output."""

    audio_base64: str
    content_type: str
    provider: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    truncated: bool = False


class VoiceTurnResponse(BaseModel):
    """Response for a turn-based voice interaction."""

    transcript: str
    response_text: str | None = None
    status: str
    run_id: int | None = None
    thread_id: int | None = None
    error: str | None = None
    stt_model: str | None = None
    tts: VoiceAudioResponse | None = None
    message_id: str | None = None


@router.post("/turn", response_model=VoiceTurnResponse)
async def voice_turn(
    audio: UploadFile = File(..., description="Audio file to transcribe"),
    stt_prompt: str | None = Form(None, description="Optional transcription prompt"),
    stt_language: str | None = Form(None, description="Optional ISO-639-1 language hint"),
    stt_model: str | None = Form(None, description="Override STT model"),
    return_audio: bool = Form(True, description="Include synthesized audio response"),
    tts_provider: str | None = Form(None, description="Override TTS provider (edge, elevenlabs)"),
    tts_voice_id: str | None = Form(None, description="Override TTS voice ID/name"),
    model: str | None = Form(None, description="Override supervisor model"),
    message_id: str | None = Form(None, description="Client-generated message ID for correlation"),
    current_user=Depends(get_current_jarvis_user),
) -> VoiceTurnResponse:
    """Turn-based voice: audio -> transcript -> supervisor response.

    This endpoint is optimized for "Alexa-style" interactions:
    - User speaks once
    - System transcribes
    - Supervisor responds with text
    """
    if not audio:
        raise HTTPException(status_code=400, detail="Audio file is required")

    normalized_content_type = normalize_content_type(audio.content_type)
    if normalized_content_type and normalized_content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported audio type: {audio.content_type}")

    audio_bytes = await audio.read()
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail=f"Audio file too large (max {MAX_AUDIO_BYTES // (1024 * 1024)}MB)")

    result = await run_voice_turn(
        owner_id=current_user.id,
        audio_bytes=audio_bytes,
        filename=audio.filename,
        content_type=audio.content_type,
        stt_prompt=stt_prompt,
        stt_language=stt_language,
        stt_model=stt_model,
        model_override=model,
        message_id=message_id,
    )

    if result.status == "error":
        detail = result.error or "Voice turn failed"
        if detail in {"Empty transcription result", "Audio too short"}:
            raise HTTPException(status_code=422, detail=detail)
        raise HTTPException(status_code=500, detail=detail)

    tts_payload: VoiceAudioResponse | None = None
    if return_audio:
        if not result.response_text:
            tts_payload = VoiceAudioResponse(
                audio_base64="",
                content_type="audio/mpeg",
                error="No response text available for TTS",
            )
        else:
            settings = get_settings()
            # In tests or when LLM calls are disabled, return a small dummy payload.
            if settings.testing or settings.llm_disabled:
                dummy_audio = b"test-audio"
                tts_payload = VoiceAudioResponse(
                    audio_base64=base64.b64encode(dummy_audio).decode("ascii"),
                    content_type="audio/mpeg",
                    provider="test",
                    latency_ms=0,
                )
            else:
                provider = None
                if tts_provider:
                    try:
                        provider = TTSProvider(tts_provider.lower())
                    except ValueError:
                        raise HTTPException(status_code=400, detail=f"Invalid TTS provider: {tts_provider}")

                tts_service = get_tts_service()
                tts_text = result.response_text
                truncated = False
                if len(tts_text) > tts_service.config.max_text_length:
                    tts_text = tts_text[: tts_service.config.max_text_length]
                    truncated = True

                tts_result = await tts_service.convert(tts_text, provider, tts_voice_id)
                if tts_result.success and tts_result.audio_data:
                    tts_payload = VoiceAudioResponse(
                        audio_base64=base64.b64encode(tts_result.audio_data).decode("ascii"),
                        content_type=tts_result.content_type,
                        provider=tts_result.provider,
                        latency_ms=tts_result.latency_ms,
                        truncated=truncated,
                    )
                else:
                    tts_payload = VoiceAudioResponse(
                        audio_base64="",
                        content_type=tts_result.content_type,
                        provider=tts_result.provider,
                        latency_ms=tts_result.latency_ms,
                        error=tts_result.error or "TTS failed",
                        truncated=truncated,
                    )

    return VoiceTurnResponse(
        transcript=result.transcript,
        response_text=result.response_text,
        status=result.status,
        run_id=result.run_id,
        thread_id=result.thread_id,
        error=result.error,
        stt_model=result.stt_model,
        tts=tts_payload,
        message_id=result.message_id,
    )
