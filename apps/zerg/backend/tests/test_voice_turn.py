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
async def test_stt_service_rejects_short_audio(monkeypatch):
    """STT should reject audio that is too short to transcribe."""
    import zerg.voice.stt_service as stt_module

    monkeypatch.setattr(
        stt_module,
        "get_settings",
        lambda: SimpleNamespace(testing=False, llm_disabled=False, openai_api_key="test-key"),
    )

    service = STTService()
    result = await service.transcribe_bytes(
        b"x" * 10,
        filename="audio.wav",
        content_type="audio/wav",
    )

    assert result.success is False
    assert result.error and "too short" in result.error.lower()


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
    # Use audio larger than MIN_AUDIO_BYTES (1024)
    result = await service.transcribe_bytes(
        b"x" * 2048,  # 2KB of audio data
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
    from zerg.routers.jarvis_auth import get_current_jarvis_user

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

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/turn",
            headers={"Authorization": "Bearer test-token"},
            files={"audio": ("sample.wav", b"audio", "audio/wav")},
            data={"return_audio": "true"},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 200
    data = response.json()
    assert data["transcript"] == "hello"
    assert data["response_text"] == "ok"
    assert data["status"] == "success"
    assert data["tts"] is not None
    assert data["tts"]["audio_base64"]


def test_voice_turn_accepts_webm_with_codecs(monkeypatch):
    """Endpoint should accept browser content-type with codec parameters."""
    import zerg.voice.router as voice_router
    from zerg.main import app
    from zerg.routers.jarvis_auth import get_current_jarvis_user

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

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/turn",
            headers={"Authorization": "Bearer test-token"},
            files={"audio": ("sample.webm", b"audio", "audio/webm;codecs=opus")},
            data={"return_audio": "true"},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 200


def test_voice_turn_empty_transcription_returns_422_with_message_id(monkeypatch):
    """Empty transcription should return 422 with status=error and message_id for correlation."""
    import zerg.voice.router as voice_router
    from zerg.main import app
    from zerg.routers.jarvis_auth import get_current_jarvis_user

    client = TestClient(app)

    monkeypatch.setattr(
        voice_router,
        "run_voice_turn",
        AsyncMock(
            return_value=VoiceTurnResult(
                transcript="",
                response_text=None,
                status="error",
                error="Empty transcription result",
            )
        ),
    )

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/turn",
            headers={"Authorization": "Bearer test-token"},
            files={"audio": ("sample.webm", b"audio", "audio/webm;codecs=opus")},
            data={"return_audio": "true", "message_id": "test-correlation-id"},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 422
    data = response.json()
    assert data["status"] == "error"
    assert data["error"] == "Empty transcription result"
    assert data["message_id"] == "test-correlation-id"


def test_voice_turn_unsupported_audio_type_returns_400_with_message_id():
    """Unsupported audio type should return 400 with message_id."""
    from zerg.main import app
    from zerg.routers.jarvis_auth import get_current_jarvis_user

    client = TestClient(app)

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/turn",
            headers={"Authorization": "Bearer test-token"},
            files={"audio": ("sample.txt", b"not audio", "text/plain")},
            data={"return_audio": "false", "message_id": "test-unsupported-type"},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 400
    data = response.json()
    assert data["status"] == "error"
    assert "Unsupported audio type" in data["error"]
    assert data["message_id"] == "test-unsupported-type"


def test_voice_turn_server_error_returns_500_with_message_id(monkeypatch):
    """Server errors should return 500 with message_id."""
    import zerg.voice.router as voice_router
    from zerg.main import app
    from zerg.routers.jarvis_auth import get_current_jarvis_user

    client = TestClient(app)

    monkeypatch.setattr(
        voice_router,
        "run_voice_turn",
        AsyncMock(
            return_value=VoiceTurnResult(
                transcript="hello",
                response_text=None,
                status="error",
                error="Internal supervisor failure",
            )
        ),
    )

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/turn",
            headers={"Authorization": "Bearer test-token"},
            files={"audio": ("sample.wav", b"audio", "audio/wav")},
            data={"return_audio": "false", "message_id": "test-server-error"},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 500
    data = response.json()
    assert data["status"] == "error"
    assert data["error"] == "Internal supervisor failure"
    assert data["message_id"] == "test-server-error"


@pytest.mark.asyncio
async def test_run_voice_turn_passes_message_id(monkeypatch):
    """Voice turn should pass message_id to supervisor and return it."""
    import zerg.voice.turn_based as turn_module

    class FakeSTT:
        async def transcribe_bytes(self, *args, **kwargs):
            return STTResult(success=True, text="hello", model="gpt-4o-mini-transcribe")

    class DummySession:
        def __enter__(self):
            return MagicMock()

        def __exit__(self, exc_type, exc, tb):
            return False

    captured_kwargs = {}

    class FakeSupervisor:
        def __init__(self, _db):
            pass

        async def run_supervisor(self, **kwargs):
            captured_kwargs.update(kwargs)
            from zerg.services.supervisor_service import SupervisorRunResult

            return SupervisorRunResult(run_id=123, thread_id=456, status="success", result="ok")

    monkeypatch.setattr(turn_module, "get_stt_service", lambda: FakeSTT())
    monkeypatch.setattr(turn_module, "db_session", lambda: DummySession())
    monkeypatch.setattr(turn_module, "SupervisorService", FakeSupervisor)

    test_message_id = "test-uuid-1234"
    result = await turn_module.run_voice_turn(
        owner_id=1,
        audio_bytes=b"audio",
        message_id=test_message_id,
    )

    assert captured_kwargs.get("message_id") == test_message_id
    assert result.message_id == test_message_id
    assert result.transcript == "hello"


def test_voice_turn_endpoint_with_message_id(monkeypatch):
    """API endpoint should accept and return message_id."""
    import zerg.voice.router as voice_router
    from zerg.main import app
    from zerg.routers.jarvis_auth import get_current_jarvis_user

    client = TestClient(app)

    test_message_id = "client-uuid-5678"
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
                message_id=test_message_id,
            )
        ),
    )

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/turn",
            headers={"Authorization": "Bearer test-token"},
            files={"audio": ("sample.wav", b"audio", "audio/wav")},
            data={"return_audio": "true", "message_id": test_message_id},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 200
    data = response.json()
    assert data["message_id"] == test_message_id
    assert data["transcript"] == "hello"


def test_voice_transcribe_endpoint_success(monkeypatch):
    """Transcribe endpoint should return transcript without supervisor execution."""
    import zerg.voice.router as voice_router
    from zerg.main import app
    from zerg.routers.jarvis_auth import get_current_jarvis_user

    client = TestClient(app)

    class FakeSTT:
        async def transcribe_bytes(self, *args, **kwargs):
            return STTResult(success=True, text="hello from stt", model="gpt-4o-mini-transcribe")

    monkeypatch.setattr(voice_router, "get_stt_service", lambda: FakeSTT())

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/transcribe",
            headers={"Authorization": "Bearer test-token"},
            files={"audio": ("sample.wav", b"audio", "audio/wav")},
            data={"message_id": "voice-msg-123"},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["transcript"] == "hello from stt"
    assert data["message_id"] == "voice-msg-123"


def test_voice_tts_endpoint_success(monkeypatch):
    """TTS endpoint should return audio payload."""
    import zerg.voice.router as voice_router
    from zerg.main import app
    from zerg.routers.jarvis_auth import get_current_jarvis_user

    client = TestClient(app)

    monkeypatch.setattr(
        voice_router,
        "get_settings",
        lambda: SimpleNamespace(testing=True, llm_disabled=False),
    )

    app.dependency_overrides[get_current_jarvis_user] = lambda: SimpleNamespace(id=1)
    try:
        response = client.post(
            "/api/jarvis/voice/tts",
            headers={"Authorization": "Bearer test-token"},
            json={"text": "hello", "message_id": "voice-msg-tts"},
        )
    finally:
        app.dependency_overrides.pop(get_current_jarvis_user, None)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["tts"]["audio_base64"]
    assert data["message_id"] == "voice-msg-tts"
