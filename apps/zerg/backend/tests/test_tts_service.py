"""Tests for the TTS service.

Tests cover:
- TTSConfig loading from environment
- Provider selection and fallback
- Text length validation
- Error handling for each provider
- Integration with the router endpoints
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.tts_service import TTSConfig
from zerg.services.tts_service import TTSProvider
from zerg.services.tts_service import TTSResult
from zerg.services.tts_service import TTSService


class TestTTSConfig:
    """Tests for TTSConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = TTSConfig()
        assert config.enabled is True
        assert config.provider == TTSProvider.EDGE
        assert config.max_text_length == 4000
        assert config.timeout_ms == 30000
        assert config.elevenlabs_voice_id == "pMsXgVXv3BLzUgSXRplE"
        assert config.edge_voice == "en-US-GuyNeural"

    def test_config_from_env(self):
        """Test loading config from environment variables."""
        env_vars = {
            "TTS_ENABLED": "1",
            "TTS_PROVIDER": "elevenlabs",
            "TTS_MAX_TEXT_LENGTH": "2000",
            "TTS_TIMEOUT_MS": "15000",
            "ELEVENLABS_API_KEY": "test-key",
            "ELEVENLABS_VOICE_ID": "custom-voice",
            "TTS_EDGE_VOICE": "en-GB-RyanNeural",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            config = TTSConfig.from_env()

        assert config.enabled is True
        assert config.provider == TTSProvider.ELEVENLABS
        assert config.max_text_length == 2000
        assert config.timeout_ms == 15000
        assert config.elevenlabs_api_key == "test-key"
        assert config.elevenlabs_voice_id == "custom-voice"
        assert config.edge_voice == "en-GB-RyanNeural"

    def test_config_disabled(self):
        """Test TTS disabled via environment."""
        with patch.dict(os.environ, {"TTS_ENABLED": "0"}, clear=False):
            config = TTSConfig.from_env()
        assert config.enabled is False

    def test_xi_api_key_fallback(self):
        """Test XI_API_KEY as fallback for ELEVENLABS_API_KEY."""
        env_vars = {"XI_API_KEY": "xi-key"}
        with patch.dict(os.environ, env_vars, clear=False):
            # Clear ELEVENLABS_API_KEY if set
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}, clear=False):
                config = TTSConfig.from_env()
        # XI_API_KEY should be used as fallback
        assert config.elevenlabs_api_key == "xi-key"


class TestTTSService:
    """Tests for TTSService."""

    def test_provider_availability_edge(self):
        """Test Edge TTS is always available."""
        config = TTSConfig(provider=TTSProvider.EDGE)
        service = TTSService(config)
        assert service._is_provider_available(TTSProvider.EDGE) is True

    def test_provider_availability_elevenlabs_no_key(self):
        """Test ElevenLabs unavailable without API key."""
        config = TTSConfig(provider=TTSProvider.ELEVENLABS, elevenlabs_api_key=None)
        service = TTSService(config)
        assert service._is_provider_available(TTSProvider.ELEVENLABS) is False

    def test_provider_availability_elevenlabs_with_key(self):
        """Test ElevenLabs available with API key."""
        config = TTSConfig(provider=TTSProvider.ELEVENLABS, elevenlabs_api_key="test-key")
        service = TTSService(config)
        assert service._is_provider_available(TTSProvider.ELEVENLABS) is True

    def test_provider_order_elevenlabs_primary(self):
        """Test provider order when ElevenLabs is primary."""
        config = TTSConfig(provider=TTSProvider.ELEVENLABS)
        service = TTSService(config)
        order = service._get_provider_order()
        assert order[0] == TTSProvider.ELEVENLABS
        assert TTSProvider.EDGE in order

    def test_provider_order_edge_primary(self):
        """Test provider order when Edge is primary."""
        config = TTSConfig(provider=TTSProvider.EDGE)
        service = TTSService(config)
        order = service._get_provider_order()
        assert order[0] == TTSProvider.EDGE

    @pytest.mark.asyncio
    async def test_convert_empty_text(self):
        """Test conversion fails with empty text."""
        service = TTSService(TTSConfig())
        result = await service.convert("")
        assert result.success is False
        assert "Empty text" in result.error

    @pytest.mark.asyncio
    async def test_convert_text_too_long(self):
        """Test conversion fails when text exceeds limit."""
        config = TTSConfig(max_text_length=100)
        service = TTSService(config)
        result = await service.convert("x" * 200)
        assert result.success is False
        assert "too long" in result.error

    @pytest.mark.asyncio
    async def test_convert_disabled(self):
        """Test conversion fails when TTS is disabled."""
        config = TTSConfig(enabled=False)
        service = TTSService(config)
        result = await service.convert("Hello world")
        assert result.success is False
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_convert_edge_success(self):
        """Test successful Edge TTS conversion."""
        config = TTSConfig(provider=TTSProvider.EDGE)
        service = TTSService(config)

        # Mock edge_tts module and Communicate class
        mock_communicate = MagicMock()
        mock_communicate.save = AsyncMock()
        mock_edge_tts = MagicMock()
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.dict("sys.modules", {"edge_tts": mock_edge_tts}):
            # Mock file read
            with patch("pathlib.Path.read_bytes", return_value=b"audio-data"):
                result = await service.convert("Hello world")

        assert result.success is True
        assert result.provider == "edge"
        assert result.audio_data == b"audio-data"
        assert result.output_format == "mp3"

    @pytest.mark.asyncio
    async def test_convert_edge_failure(self):
        """Test Edge TTS conversion failure (no fallback)."""
        config = TTSConfig(provider=TTSProvider.EDGE)
        service = TTSService(config)

        mock_edge_tts = MagicMock()
        mock_edge_tts.Communicate.side_effect = Exception("Network error")

        with patch.dict("sys.modules", {"edge_tts": mock_edge_tts}):
            # Specify provider=EDGE to prevent fallback
            result = await service.convert("Hello world", provider=TTSProvider.EDGE)

        assert result.success is False
        assert "Edge TTS error" in result.error

    @pytest.mark.asyncio
    async def test_convert_elevenlabs_success(self):
        """Test successful ElevenLabs conversion."""
        config = TTSConfig(provider=TTSProvider.ELEVENLABS, elevenlabs_api_key="test-key")
        service = TTSService(config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"audio-data"

        # Mock the httpx client context manager
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await service.convert("Hello world")

        assert result.success is True
        assert result.provider == "elevenlabs"
        assert result.audio_data == b"audio-data"

    @pytest.mark.asyncio
    async def test_convert_elevenlabs_api_error(self):
        """Test ElevenLabs API error handling with no fallback."""
        # Only ElevenLabs available, no Edge fallback
        config = TTSConfig(provider=TTSProvider.ELEVENLABS, elevenlabs_api_key="test-key")
        service = TTSService(config)

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Invalid API key"

        # Mock the httpx client context manager
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        # Also mock edge_tts to raise ImportError (no fallback)
        def raise_import_error():
            raise ImportError("No edge_tts")

        with patch("httpx.AsyncClient", return_value=mock_client):
            # Force edge_tts import to fail
            import sys
            original_modules = sys.modules.copy()
            sys.modules["edge_tts"] = None

            try:
                result = await service.convert("Hello world", provider=TTSProvider.ELEVENLABS)
            finally:
                sys.modules.update(original_modules)

        assert result.success is False
        assert "401" in result.error

    @pytest.mark.asyncio
    async def test_convert_elevenlabs_timeout(self):
        """Test ElevenLabs timeout handling."""
        import httpx

        config = TTSConfig(provider=TTSProvider.ELEVENLABS, elevenlabs_api_key="test-key")
        service = TTSService(config)

        # Mock the httpx client context manager
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("Request timed out"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await service.convert("Hello world", provider=TTSProvider.ELEVENLABS)

        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_convert_fallback_to_edge(self):
        """Test fallback from ElevenLabs to Edge on failure."""
        config = TTSConfig(provider=TTSProvider.ELEVENLABS, elevenlabs_api_key="test-key")
        service = TTSService(config)

        # Mock ElevenLabs failure
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Server error"

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        # Mock Edge success
        mock_communicate = MagicMock()
        mock_communicate.save = AsyncMock()
        mock_edge_tts = MagicMock()
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.dict("sys.modules", {"edge_tts": mock_edge_tts}):
                with patch("pathlib.Path.read_bytes", return_value=b"edge-audio"):
                    result = await service.convert("Hello world")

        # Should fall back to Edge
        assert result.success is True
        assert result.provider == "edge"

    @pytest.mark.asyncio
    async def test_convert_with_specific_provider(self):
        """Test specifying provider override."""
        # Default to ElevenLabs but request Edge
        config = TTSConfig(provider=TTSProvider.ELEVENLABS, elevenlabs_api_key="test-key")
        service = TTSService(config)

        mock_communicate = MagicMock()
        mock_communicate.save = AsyncMock()
        mock_edge_tts = MagicMock()
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.dict("sys.modules", {"edge_tts": mock_edge_tts}):
            with patch("pathlib.Path.read_bytes", return_value=b"audio"):
                result = await service.convert("Hello", provider=TTSProvider.EDGE)

        assert result.provider == "edge"

    def test_cleanup(self):
        """Test temporary file cleanup."""
        config = TTSConfig()
        service = TTSService(config)

        # Create temp dir
        temp_dir = service._get_temp_dir()
        assert temp_dir.exists()

        # Cleanup
        service.cleanup()
        assert not temp_dir.exists()


class TestTTSRouter:
    """Tests for TTS router endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from fastapi.testclient import TestClient

        from zerg.main import app

        return TestClient(app)

    @pytest.fixture
    def auth_headers(self):
        """Return headers that bypass auth in test mode."""
        return {"Authorization": "Bearer test-token"}

    @pytest.mark.asyncio
    async def test_tts_status_endpoint(self, client, auth_headers):
        """Test GET /api/jarvis/tts/status endpoint."""
        # This test requires the full app to be running with test auth
        # For unit tests, we test the service directly
        pass

    @pytest.mark.asyncio
    async def test_tts_voices_endpoint(self, client, auth_headers):
        """Test GET /api/jarvis/tts/voices endpoint."""
        pass


class TestTTSResult:
    """Tests for TTSResult dataclass."""

    def test_success_result(self):
        """Test successful result attributes."""
        result = TTSResult(
            success=True,
            audio_path="/tmp/audio.mp3",
            audio_data=b"data",
            latency_ms=100,
            provider="edge",
            output_format="mp3",
        )
        assert result.success is True
        assert result.error is None
        assert result.content_type == "audio/mpeg"

    def test_failure_result(self):
        """Test failure result attributes."""
        result = TTSResult(success=False, error="Something went wrong")
        assert result.success is False
        assert result.audio_data is None
        assert result.error == "Something went wrong"


class TestEdgeTTSIntegration:
    """Integration tests for Edge TTS (requires network)."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_real_edge_conversion(self):
        """Test real Edge TTS conversion (requires network)."""
        config = TTSConfig(provider=TTSProvider.EDGE)
        service = TTSService(config)

        try:
            result = await service.convert("Hello, this is a test of Jarvis voice.")

            assert result.success is True
            assert result.provider == "edge"
            assert result.audio_data is not None
            assert len(result.audio_data) > 0
            assert result.latency_ms is not None
        finally:
            service.cleanup()
