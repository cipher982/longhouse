"""Startup validation + capability checks driven by config/models.json."""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest


@pytest.fixture(autouse=True)
def _restore_shared_models_config(monkeypatch):
    """Keep fixture-driven module reloads from leaking into later test files."""

    import zerg.models_config as models_config

    original_path = models_config._get_config_path()
    yield
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(original_path))
    importlib.reload(models_config)


def _write_test_config(tmp_path, *, with_summary_update=True):
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
            "text": {"primary": "TIER_1", "test": "TIER_2"},
            "realtime": {},
        },
    }
    if with_summary_update:
        config["useCases"]["text"]["summary_update"] = "TIER_2"
    config["embedding"] = {
        "default": {
            "provider": "local-onnx",
            "model": "fake/embed",
            "dims": 64,
            "dtype": "float32",
            "normalization": "l2",
            "truncation": "prefix",
            "outputName": "sentence_embedding",
            "queryPrefix": "query: ",
            "documentPrefix": "document: ",
            "legacyModels": ["fake/legacy"],
            "artifact": {
                "repository": "fake/embed",
                "revision": "0" * 40,
                "directory": "fake-embed",
                "files": [
                    {
                        "path": "model.onnx",
                        "bytes": 1,
                        "sha256": "0" * 64,
                    }
                ],
            },
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(config))
    return path


def _reload_models_config(monkeypatch, config_path):
    """Reload models_config with MODELS_CONFIG_PATH pointing at fixture."""
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
    assert "config/models.json" in msg


def test_is_capability_available_text_requires_active_provider_key(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mc = _reload_models_config(monkeypatch, cfg)

    assert mc.is_capability_available("text") is False

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert mc.is_capability_available("text") is True


def test_openrouter_client_enforces_provider_data_collection_deny(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    mc = _reload_models_config(monkeypatch, cfg)

    client, _model, provider = mc.get_llm_client_for_use_case("summarization")
    try:
        assert provider == mc.ModelProvider.OPENROUTER
        assert mc.llm_request_policy_kwargs(client) == {"extra_body": {"provider": {"data_collection": "deny"}}}
        first = mc.llm_request_policy_kwargs(client)
        first["extra_body"]["provider"]["data_collection"] = "allow"
        assert mc.llm_request_policy_kwargs(client)["extra_body"]["provider"]["data_collection"] == "deny"
    finally:
        asyncio.run(client.close())


def test_is_capability_available_embedding_requires_local_contract_not_key(tmp_path, monkeypatch):
    cfg = _write_test_config(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mc = _reload_models_config(monkeypatch, cfg)

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


def test_lifespan_model_validation_warns_and_boots_when_no_llm_keys(tmp_path, monkeypatch, caplog):
    """True first-run (no provider keys at all) boots with a visible banner.

    Honors the Settings.llm_available contract: 'UI boots without API keys,
    but chat features prompt for configuration.' Operators see a multi-line
    warning naming the disabled capabilities and how to enable them.
    """
    import logging
    from types import SimpleNamespace

    monkeypatch.setenv("TESTING", "1")
    cfg = _write_test_config(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _reload_models_config(monkeypatch, cfg)

    import zerg.lifespan as lifespan

    monkeypatch.setattr(
        lifespan,
        "_settings",
        SimpleNamespace(testing=False, llm_disabled=False, demo_mode=False, llm_available=False),
    )

    with caplog.at_level(logging.WARNING, logger="zerg.lifespan"):
        lifespan._validate_models_config_startup()  # must not raise

    banner = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "LIMITED MODE" in banner
    assert "LLM_DISABLED=1" in banner
    assert "summarization" in banner


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
