"""Turn-based voice orchestration: STT -> supervisor -> text response."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from zerg.config import get_settings
from zerg.database import db_session
from zerg.services.supervisor_service import SupervisorService
from zerg.voice.stt_service import STTResult
from zerg.voice.stt_service import get_stt_service

logger = logging.getLogger(__name__)


@dataclass
class VoiceTurnResult:
    """Result for a turn-based voice interaction."""

    transcript: str
    response_text: str | None
    status: str
    run_id: int | None = None
    thread_id: int | None = None
    error: str | None = None
    stt_model: str | None = None
    message_id: str | None = None


async def run_voice_turn(
    *,
    owner_id: int,
    audio_bytes: bytes,
    filename: str | None = None,
    content_type: str | None = None,
    stt_prompt: str | None = None,
    stt_language: str | None = None,
    stt_model: str | None = None,
    model_override: str | None = None,
    message_id: str | None = None,
) -> VoiceTurnResult:
    """Execute a single voice turn.

    Steps:
      1) Transcribe audio to text
      2) Run supervisor on the transcript
      3) Return transcript + response text
    """
    stt_service = get_stt_service()
    stt_result: STTResult = await stt_service.transcribe_bytes(
        audio_bytes,
        filename=filename,
        content_type=content_type,
        prompt=stt_prompt,
        language=stt_language,
        model=stt_model,
    )

    if not stt_result.success or not stt_result.text:
        return VoiceTurnResult(
            transcript="",
            response_text=None,
            status="error",
            error=stt_result.error or "STT failed",
            stt_model=stt_result.model,
        )

    settings = get_settings()
    effective_model = model_override
    if effective_model is None and settings.testing:
        effective_model = "gpt-scripted"

    try:
        with db_session() as db:
            supervisor = SupervisorService(db)
            result = await supervisor.run_supervisor(
                owner_id=owner_id,
                task=stt_result.text,
                model_override=effective_model,
                message_id=message_id,
            )

        return VoiceTurnResult(
            transcript=stt_result.text,
            response_text=result.result,
            status=result.status,
            run_id=result.run_id,
            thread_id=result.thread_id,
            stt_model=stt_result.model,
            message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Voice turn failed")
        return VoiceTurnResult(
            transcript=stt_result.text,
            response_text=None,
            status="error",
            error=str(exc),
            stt_model=stt_result.model,
        )
