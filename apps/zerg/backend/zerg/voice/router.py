"""Jarvis turn-based voice endpoints."""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from zerg.config import get_settings
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.voice.stt_service import ALLOWED_AUDIO_TYPES
from zerg.voice.stt_service import MAX_AUDIO_BYTES
from zerg.voice.stt_service import get_stt_service
from zerg.voice.stt_service import normalize_content_type
from zerg.voice.tts_service import TTSProvider
from zerg.voice.tts_service import get_tts_service
from zerg.voice.turn_based import run_voice_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["jarvis-voice"])


def _voice_turn_error(
    status_code: int,
    error: str,
    message_id: str | None = None,
    transcript: str = "",
    stt_model: str | None = None,
) -> JSONResponse:
    """Return voice turn error with proper HTTP status AND message_id for correlation."""
    return JSONResponse(
        status_code=status_code,
        content={
            "transcript": transcript,
            "response_text": None,
            "status": "error",
            "run_id": None,
            "thread_id": None,
            "error": error,
            "stt_model": stt_model,
            "tts": None,
            "message_id": message_id,
        },
    )


def _transcribe_error(
    status_code: int,
    error: str,
    message_id: str | None = None,
    stt_model: str | None = None,
) -> JSONResponse:
    """Return transcribe error with proper HTTP status AND message_id."""
    return JSONResponse(
        status_code=status_code,
        content={
            "transcript": "",
            "status": "error",
            "error": error,
            "stt_model": stt_model,
            "message_id": message_id,
        },
    )


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


class VoiceTranscribeResponse(BaseModel):
    """Response for a voice transcription request."""

    transcript: str
    status: str
    error: str | None = None
    stt_model: str | None = None
    message_id: str | None = None


class VoiceTtsRequest(BaseModel):
    """Request payload for text-to-speech."""

    text: str
    provider: str | None = None
    voice_id: str | None = None
    message_id: str | None = None


class VoiceTtsResponse(BaseModel):
    """Response for text-to-speech."""

    status: str
    tts: VoiceAudioResponse | None = None
    error: str | None = None
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
        return _voice_turn_error(400, "Audio file is required", message_id)

    normalized_content_type = normalize_content_type(audio.content_type)
    if normalized_content_type and normalized_content_type not in ALLOWED_AUDIO_TYPES:
        return _voice_turn_error(400, f"Unsupported audio type: {audio.content_type}", message_id)

    audio_bytes = await audio.read()
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return _voice_turn_error(413, f"Audio file too large (max {MAX_AUDIO_BYTES // (1024 * 1024)}MB)", message_id)

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
        # Use 422 for expected STT errors (user can retry), 500 for unexpected failures
        error_msg = result.error or "Voice turn failed"
        if error_msg in {"Empty transcription result", "Audio too short", "STT failed"}:
            status_code = 422
        else:
            status_code = 500
        return _voice_turn_error(
            status_code,
            error_msg,
            message_id,
            transcript=result.transcript or "",
            stt_model=result.stt_model,
        )

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
                        return _voice_turn_error(
                            400,
                            f"Invalid TTS provider: {tts_provider}",
                            message_id,
                            transcript=result.transcript,
                            stt_model=result.stt_model,
                        )

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


@router.post("/transcribe", response_model=VoiceTranscribeResponse)
async def voice_transcribe(
    audio: UploadFile = File(..., description="Audio file to transcribe"),
    stt_prompt: str | None = Form(None, description="Optional transcription prompt"),
    stt_language: str | None = Form(None, description="Optional ISO-639-1 language hint"),
    stt_model: str | None = Form(None, description="Override STT model"),
    message_id: str | None = Form(None, description="Client-generated message ID for correlation"),
    current_user=Depends(get_current_jarvis_user),
) -> VoiceTranscribeResponse:
    """Transcribe audio to text (no supervisor execution)."""
    if not audio:
        return _transcribe_error(400, "Audio file is required", message_id)

    normalized_content_type = normalize_content_type(audio.content_type)
    if normalized_content_type and normalized_content_type not in ALLOWED_AUDIO_TYPES:
        return _transcribe_error(400, f"Unsupported audio type: {audio.content_type}", message_id)

    audio_bytes = await audio.read()
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return _transcribe_error(413, f"Audio file too large (max {MAX_AUDIO_BYTES // (1024 * 1024)}MB)", message_id)

    stt_service = get_stt_service()
    result = await stt_service.transcribe_bytes(
        audio_bytes,
        filename=audio.filename,
        content_type=audio.content_type,
        prompt=stt_prompt,
        language=stt_language,
        model=stt_model,
    )

    if not result.success or not result.text:
        return VoiceTranscribeResponse(
            transcript="",
            status="error",
            error=result.error or "STT failed",
            stt_model=result.model,
            message_id=message_id,
        )

    return VoiceTranscribeResponse(
        transcript=result.text,
        status="success",
        stt_model=result.model,
        message_id=message_id,
    )


@router.post("/tts", response_model=VoiceTtsResponse)
async def voice_tts(
    request: VoiceTtsRequest,
    current_user=Depends(get_current_jarvis_user),
) -> VoiceTtsResponse:
    """Convert text to speech (no supervisor execution)."""
    text = (request.text or "").strip()
    if not text:
        return VoiceTtsResponse(
            status="error",
            error="Text is required",
            message_id=request.message_id,
        )

    settings = get_settings()
    if settings.testing or settings.llm_disabled:
        dummy_audio = b"test-audio"
        return VoiceTtsResponse(
            status="success",
            tts=VoiceAudioResponse(
                audio_base64=base64.b64encode(dummy_audio).decode("ascii"),
                content_type="audio/mpeg",
                provider="test",
                latency_ms=0,
            ),
            message_id=request.message_id,
        )

    provider = None
    if request.provider:
        try:
            provider = TTSProvider(request.provider.lower())
        except ValueError:
            return VoiceTtsResponse(
                status="error",
                error=f"Invalid TTS provider: {request.provider}",
                message_id=request.message_id,
            )

    tts_service = get_tts_service()
    tts_text = text
    truncated = False
    if len(tts_text) > tts_service.config.max_text_length:
        tts_text = tts_text[: tts_service.config.max_text_length]
        truncated = True

    tts_result = await tts_service.convert(tts_text, provider, request.voice_id)
    if tts_result.success and tts_result.audio_data:
        tts_payload = VoiceAudioResponse(
            audio_base64=base64.b64encode(tts_result.audio_data).decode("ascii"),
            content_type=tts_result.content_type,
            provider=tts_result.provider,
            latency_ms=tts_result.latency_ms,
            truncated=truncated,
        )
        return VoiceTtsResponse(
            status="success",
            tts=tts_payload,
            message_id=request.message_id,
        )

    tts_payload = VoiceAudioResponse(
        audio_base64="",
        content_type=tts_result.content_type,
        provider=tts_result.provider,
        latency_ms=tts_result.latency_ms,
        error=tts_result.error or "TTS failed",
        truncated=truncated,
    )
    return VoiceTtsResponse(
        status="error",
        tts=tts_payload,
        error=tts_payload.error,
        message_id=request.message_id,
    )
