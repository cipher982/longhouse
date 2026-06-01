"""Startup validation + capability checks driven by config/models.json."""

from __future__ import annotations

import json

import pytest


def _write_test_config(tmp_path, *, with_embedding=True, with_summary_update=True):
    config = {
        "text": {
            "tiers": {"TIER_1": "fake/pro", "TIER_2": "fake/flash", "TIER_3": "fake/flash"},
            "models": {
                "fake/pro": {
                    "displayName": "Fake Pro",
                    "provider": "openrouter",
                    "tier": "TIER_1",
                    "apiKeyEnvVar": "OPENROUTER_API_KEY",
                },
                "fake/flash": {
                    "displayName": "Fake Flash",
                    "provider": "openrouter",
                    "tier": "TIER_2",
                    "apiKeyEnvVar": "OPENROUTER_API_KEY",
                },
            },
        },
        "useCases": {
            "text": {"summarization": "TIER_2"},
            "realtime": {},
        },
        "defaults": {
            "text": {"fiche": "TIER_1", "test": "TIER_2"},
            "realtime": {},
        },
    }
    if with_summary_update:
        config["useCases"]["text"]["summary_update"] = "TIER_2"
    if with_embedding:
        config["embedding"] = {
            "default": {
                "provider": "openrouter",
                "model": "fake/embed",
                "dims": 64,
                "apiKeyEnvVar": "OPENROUTER_API_KEY",
            }
        }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(config))
    return path


def _reload_models_config(monkeypatch, config_path):
    """Reload models_config with MODELS_CONFIG_PATH pointing at fixture."""
    import importlib

    monkeypatch.setenv("MODELS_CONFIG_PATH", str(config_path))
    import zerg.models_config as mc

    return importlib.reload(mc)


def test_validate_startup_config_passes_when_keys_present(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    mc = _reload_models_config(monkeypatch, cfg)

    mc.validate_startup_config()  # no raise


def test_validate_startup_config_raises_with_actionable_message(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mc = _reload_models_config(monkeypatch, cfg)

    with pytest.raises(RuntimeError) as exc_info:
        mc.validate_startup_config()

    msg = str(exc_info.value)
    assert "OPENROUTER_API_KEY" in msg
    assert "use case 'summarization'" in msg
    assert "embedding" in msg
    assert "config/models.json" in msg


def test_is_capability_available_text_requires_active_provider_key(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path, with_embedding=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mc = _reload_models_config(monkeypatch, cfg)

    assert mc.is_capability_available("text") is False

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert mc.is_capability_available("text") is True


def test_is_capability_available_embedding_returns_false_when_unconfigured(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path, with_embedding=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    mc = _reload_models_config(monkeypatch, cfg)

    assert mc.is_capability_available("embedding") is False


def test_is_capability_available_embedding_requires_key(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mc = _reload_models_config(monkeypatch, cfg)

    assert mc.is_capability_available("embedding") is False

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert mc.is_capability_available("embedding") is True


def test_is_capability_available_rejects_unknown_capability(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    mc = _reload_models_config(monkeypatch, cfg)

    with pytest.raises(ValueError):
        mc.is_capability_available("realtime")


def test_lifespan_model_validation_skips_when_llm_disabled(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("TESTING", "1")
    import zerg.lifespan as lifespan

    monkeypatch.setattr(
        lifespan,
        "_settings",
        SimpleNamespace(testing=False, llm_disabled=True, demo_mode=False, llm_available=False),
    )
    monkeypatch.setattr(
        "zerg.models_config.validate_startup_config",
        lambda: (_ for _ in ()).throw(AssertionError("validation should be skipped")),
    )

    lifespan._validate_models_config_startup()


def test_lifespan_model_validation_skips_when_no_llm_keys(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("TESTING", "1")
    import zerg.lifespan as lifespan

    monkeypatch.setattr(
        lifespan,
        "_settings",
        SimpleNamespace(testing=False, llm_disabled=False, demo_mode=False, llm_available=False),
    )
    monkeypatch.setattr(
        "zerg.models_config.validate_startup_config",
        lambda: (_ for _ in ()).throw(AssertionError("validation should be skipped")),
    )

    lifespan._validate_models_config_startup()


def test_lifespan_model_validation_raises_when_enabled(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("TESTING", "1")
    cfg = _write_test_config(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _reload_models_config(monkeypatch, cfg)

    import zerg.lifespan as lifespan

    monkeypatch.setattr(
        lifespan,
        "_settings",
        SimpleNamespace(testing=False, llm_disabled=False, demo_mode=False, llm_available=True),
    )

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        lifespan._validate_models_config_startup()
