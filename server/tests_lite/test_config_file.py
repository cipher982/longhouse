from __future__ import annotations

from zerg.cli.config_file import config_to_dict
from zerg.cli.config_file import load_config
from zerg.cli.config_file import save_loaded_config


def test_load_config_ignores_legacy_url_mirrors(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                'host = "0.0.0.0"',
                "port = 9090",
                'public_url = "https://longhouse.example.com"',
                "",
                "[browser]",
                'default_url = "https://stale-browser.example.com"',
                "",
                "[shipper]",
                'api_url = "https://stale-shipper.example.com"',
                "fallback_scan_secs = 120",
                "",
            ]
        )
    )

    config = load_config(config_path=config_path)

    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9090
    assert config.server.public_url == "https://longhouse.example.com"
    assert config.shipper.fallback_scan_secs == 120
    assert "browser.default_url" not in config._sources
    assert "shipper.api_url" not in config._sources


def test_save_loaded_config_drops_legacy_url_mirrors(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[browser]",
                'default_url = "https://stale-browser.example.com"',
                "",
                "[shipper]",
                'api_url = "https://stale-shipper.example.com"',
                "fallback_scan_secs = 120",
                "",
            ]
        )
    )

    config = load_config(config_path=config_path)
    save_loaded_config(config, config_path=config_path)

    saved = config_path.read_text()

    assert "[browser]" not in saved
    assert "default_url" not in saved
    assert "api_url" not in saved
    assert config_to_dict(config) == {
        "server": {
            "host": "127.0.0.1",
            "port": 8080,
            "public_url": None,
        },
        "shipper": {
            "fallback_scan_secs": 120,
        },
    }
