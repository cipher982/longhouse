from pathlib import Path

import zerg.models_config as models_config


def test_get_config_path_prefers_packaged_copy_when_present(tmp_path, monkeypatch):
    fake_module = tmp_path / "site-packages" / "zerg" / "models_config.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# fake module path\n", encoding="utf-8")

    packaged_config = fake_module.parent / "_config" / "models.json"
    packaged_config.parent.mkdir(parents=True)
    packaged_config.write_text('{"text":{"tiers":{},"models":{}},"useCases":{"text":{}},"defaults":{"text":{}}}', encoding="utf-8")

    monkeypatch.delenv("MODELS_CONFIG_PATH", raising=False)
    monkeypatch.setattr(models_config, "__file__", str(fake_module))

    assert models_config._get_config_path() == packaged_config


def test_get_config_path_falls_back_to_repo_layout(tmp_path, monkeypatch):
    fake_module = tmp_path / "repo" / "apps" / "zerg" / "backend" / "zerg" / "models_config.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# fake module path\n", encoding="utf-8")

    repo_config = tmp_path / "repo" / "config" / "models.json"
    repo_config.parent.mkdir(parents=True)
    repo_config.write_text('{"text":{"tiers":{},"models":{}},"useCases":{"text":{}},"defaults":{"text":{}}}', encoding="utf-8")

    monkeypatch.delenv("MODELS_CONFIG_PATH", raising=False)
    monkeypatch.setattr(models_config, "__file__", str(fake_module))

    assert models_config._get_config_path() == repo_config
