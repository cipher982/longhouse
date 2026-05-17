from __future__ import annotations

import os

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.config import get_settings
from zerg.main import _settings
from zerg.main import app


def test_get_settings_uses_umami_env_with_legacy_vite_fallback(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
    monkeypatch.delenv("UMAMI_WEBSITE_ID", raising=False)
    monkeypatch.delenv("UMAMI_SCRIPT_SRC", raising=False)
    monkeypatch.delenv("UMAMI_DOMAINS", raising=False)
    monkeypatch.setenv("VITE_UMAMI_WEBSITE_ID", "demo-site")
    monkeypatch.setenv("VITE_UMAMI_SCRIPT_SRC", "https://analytics.example/script.js")
    monkeypatch.setenv("VITE_UMAMI_DOMAINS", "longhouse.ai")

    settings = get_settings()

    assert settings.umami_website_id == "demo-site"
    assert settings.umami_script_src == "https://analytics.example/script.js"
    assert settings.umami_domains == "longhouse.ai"


def test_get_settings_preserves_explicit_empty_umami_override(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("UMAMI_WEBSITE_ID", "")
    monkeypatch.setenv("UMAMI_SCRIPT_SRC", "")
    monkeypatch.setenv("UMAMI_DOMAINS", "")
    monkeypatch.setenv("VITE_UMAMI_WEBSITE_ID", "demo-site")
    monkeypatch.setenv("VITE_UMAMI_SCRIPT_SRC", "https://analytics.example/script.js")
    monkeypatch.setenv("VITE_UMAMI_DOMAINS", "longhouse.ai")

    settings = get_settings()

    assert settings.umami_website_id == ""
    assert settings.umami_script_src == ""
    assert settings.umami_domains == ""


def test_config_js_includes_runtime_umami_values(monkeypatch):
    monkeypatch.setattr(_settings, "app_public_url", "https://longhouse.ai")
    monkeypatch.setattr(_settings, "public_site_url", "https://longhouse.ai")
    monkeypatch.setattr(_settings, "control_plane_url", None)
    monkeypatch.setattr(_settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(_settings, "single_tenant", True)
    monkeypatch.setattr(_settings, "auth_disabled", False)
    monkeypatch.setattr(_settings, "umami_website_id", "demo-site")
    monkeypatch.setattr(_settings, "umami_script_src", "https://analytics.example/script.js")
    monkeypatch.setattr(_settings, "umami_domains", "longhouse.ai")
    monkeypatch.setattr(_settings, "umami_tag", "prod")
    monkeypatch.setattr(_settings, "openai_api_key", "test-openai-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    # Active text use cases route to OpenRouter (per config/models.json), and
    # the embedding default also lives on OpenRouter. Both capability flags
    # are derived from that config + env-var presence.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    with TestClient(app) as client:
        response = client.get("/config.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert 'window.API_BASE_URL="/api";' in response.text
    assert 'window.WS_BASE_URL="wss://longhouse.ai";' in response.text
    assert 'window.__GOOGLE_CLIENT_ID__="google-client-id";' in response.text
    assert "window.__SINGLE_TENANT__=true;" in response.text
    assert "window.__LLM_AVAILABLE__=true;" in response.text
    assert "window.__EMBEDDINGS_AVAILABLE__=true;" in response.text
    assert 'window.__UMAMI_WEBSITE_ID__="demo-site";' in response.text
    assert 'window.__UMAMI_SCRIPT_SRC__="https://analytics.example/script.js";' in response.text
    assert 'window.__UMAMI_DOMAINS__="longhouse.ai";' in response.text
    assert 'window.__UMAMI_TAG__="prod";' in response.text
