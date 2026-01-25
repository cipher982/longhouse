"""Jarvis turn-based voice endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from pydantic import BaseModel

from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.voice.stt_service import ALLOWED_AUDIO_TYPES
from zerg.voice.stt_service import MAX_AUDIO_BYTES
from zerg.voice.turn_based import run_voice_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["jarvis-voice"])


class VoiceTurnResponse(BaseModel):
    """Response for a turn-based voice interaction."""

    transcript: str
    response_text: str | None = None
    status: str
    run_id: int | None = None
    thread_id: int | None = None
    error: str | None = None
    stt_model: str | None = None


@router.post("/turn", response_model=VoiceTurnResponse)
async def voice_turn(
    audio: UploadFile = File(..., description="Audio file to transcribe"),
    stt_prompt: str | None = Form(None, description="Optional transcription prompt"),
    stt_language: str | None = Form(None, description="Optional ISO-639-1 language hint"),
    stt_model: str | None = Form(None, description="Override STT model"),
    model: str | None = Form(None, description="Override supervisor model"),
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

    if audio.content_type and audio.content_type not in ALLOWED_AUDIO_TYPES:
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
    )

    if result.status == "error":
        raise HTTPException(status_code=500, detail=result.error or "Voice turn failed")

    return VoiceTurnResponse(
        transcript=result.transcript,
        response_text=result.response_text,
        status=result.status,
        run_id=result.run_id,
        thread_id=result.thread_id,
        error=result.error,
        stt_model=result.stt_model,
    )
