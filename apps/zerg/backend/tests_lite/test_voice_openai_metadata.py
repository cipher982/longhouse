from zerg.voice.openai_metadata import get_openai_audio_extra_body


def test_openai_audio_extra_body_uses_explicit_override(monkeypatch):
    monkeypatch.setenv("OPENAI_METADATA_SOURCE", "manual:test-source")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    assert get_openai_audio_extra_body("longhouse:voice-stt") == {
        "metadata": {"source": "manual:test-source"}
    }


def test_openai_audio_extra_body_uses_proxy_host(monkeypatch):
    monkeypatch.delenv("OPENAI_METADATA_SOURCE", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.drose.io/v1")

    assert get_openai_audio_extra_body("longhouse:voice-stt") == {
        "metadata": {"source": "longhouse:voice-stt"}
    }


def test_openai_audio_extra_body_uses_litellm_key_prefix(monkeypatch):
    monkeypatch.delenv("OPENAI_METADATA_SOURCE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    assert get_openai_audio_extra_body("longhouse:voice-tts", api_key="sk-litellm-test") == {
        "metadata": {"source": "longhouse:voice-tts"}
    }


def test_openai_audio_extra_body_returns_none_for_direct_openai(monkeypatch):
    monkeypatch.delenv("OPENAI_METADATA_SOURCE", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    assert get_openai_audio_extra_body("longhouse:voice-tts", api_key="sk-openai-test") is None
