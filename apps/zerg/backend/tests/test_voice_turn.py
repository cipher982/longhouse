"""Tests for turn-based voice (STT + supervisor)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zerg.voice.stt_service import MAX_AUDIO_BYTES
from zerg.voice.stt_service import STTResult
from zerg.voice.stt_service import STTService
from zerg.voice.turn_based import VoiceTurnResult


@pytest.mark.asyncio
async def test_stt_service_rejects_large_audio(monkeypatch):
    """STT should reject audio larger than the 25MB limit."""
    import zerg.voice.stt_service as stt_module

    monkeypatch.setattr(
        stt_module,
        "get_settings",
        lambda: SimpleNamespace(testing=False, llm_disabled=False, openai_api_key="test-key"),
    )

    service = STTService()
    result = await service.transcribe_bytes(
        b"x" * (MAX_AUDIO_BYTES + 1),
        filename="audio.wav",
        content_type="audio/wav",
    )

    assert result.success is False
    assert result.error and "too large" in result.error.lower()


@pytest.mark.asyncio
async def test_stt_service_calls_openai(monkeypatch):
    """STT should call OpenAI when not in testing mode."""
    import zerg.voice.stt_service as stt_module

    monkeypatch.setattr(
        stt_module,
        "get_settings",
        lambda: SimpleNamespace(testing=False, llm_disabled=False, openai_api_key="test-key"),
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "hello transcript"
    mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(stt_module, "AsyncOpenAI", lambda api_key=None: mock_client)

    service = STTService()
    result = await service.transcribe_bytes(
        b"audio-bytes",
        filename="audio.wav",
        content_type="audio/wav",
    )

    assert result.success is True
    assert result.text == "hello transcript"
    mock_client.audio.transcriptions.create.assert_called_once()


@pytest.mark.asyncio
async def test_run_voice_turn_happy_path(monkeypatch):
    """Turn-based voice should combine STT + supervisor response."""
    import zerg.voice.turn_based as turn_module

    class FakeSTT:
        async def transcribe_bytes(self, *args, **kwargs):
            return STTResult(success=True, text="hello", model="gpt-4o-mini-transcribe")

    class DummySession:
        def __enter__(self):
            return MagicMock()

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeSupervisor:
        def __init__(self, _db):
            pass

        async def run_supervisor(self, **kwargs):
            from zerg.services.supervisor_service import SupervisorRunResult

            return SupervisorRunResult(run_id=123, thread_id=456, status="success", result="ok")

    monkeypatch.setattr(turn_module, "get_stt_service", lambda: FakeSTT())
    monkeypatch.setattr(turn_module, "db_session", lambda: DummySession())
    monkeypatch.setattr(turn_module, "SupervisorService", FakeSupervisor)

    result = await turn_module.run_voice_turn(owner_id=1, audio_bytes=b"audio")

    assert result.transcript == "hello"
    assert result.response_text == "ok"
    assert result.status == "success"
    assert result.run_id == 123
    assert result.thread_id == 456


def test_voice_turn_endpoint_success(monkeypatch):
    """API endpoint should return transcript + response."""
    import zerg.voice.router as voice_router
    from zerg.main import app

    client = TestClient(app)

    monkeypatch.setattr(
        voice_router,
        "run_voice_turn",
        AsyncMock(
            return_value=VoiceTurnResult(
                transcript="hello",
                response_text="ok",
                status="success",
                run_id=1,
                thread_id=2,
                stt_model="gpt-4o-mini-transcribe",
            )
        ),
    )

    response = client.post(
        "/api/jarvis/voice/turn",
        headers={"Authorization": "Bearer test-token"},
        files={"audio": ("sample.wav", b"audio", "audio/wav")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["transcript"] == "hello"
    assert data["response_text"] == "ok"
    assert data["status"] == "success"
