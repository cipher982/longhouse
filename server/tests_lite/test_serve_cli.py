from __future__ import annotations

import os
import sys
from types import ModuleType

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import config_file as config_file_cli
from zerg.cli import serve as serve_cli


class _FakeSocket:
    def bind(self, _addr) -> None:
        return None

    def close(self) -> None:
        return None


def _base_config(*, public_url: str | None) -> config_file_cli.LonghouseConfig:
    return config_file_cli.LonghouseConfig(
        server=config_file_cli.ServerConfig(host="127.0.0.1", port=8080, public_url=public_url),
        shipper=config_file_cli.ShipperConfig(fallback_scan_secs=300),
    )


def _patch_serve(monkeypatch, tmp_path, *, config):
    uvicorn_calls: list[tuple[tuple, dict]] = []
    saved_configs: list[dict] = []

    monkeypatch.setattr(
        serve_cli,
        "_apply_lite_mode_defaults",
        lambda *, public_intent=False: os.environ.setdefault("DATABASE_URL", "sqlite:///tmp/test.db"),
    )
    monkeypatch.setattr(serve_cli, "_get_lan_ip", lambda: "192.168.1.42")

    import socket as socket_mod

    monkeypatch.setattr(socket_mod, "socket", lambda *args, **kwargs: _FakeSocket())

    fake_main = ModuleType("zerg.main")
    fake_main.FRONTEND_DIST_DIR = tmp_path
    fake_main.FRONTEND_SOURCE = "bundled"
    fake_main.app = object()
    monkeypatch.setitem(sys.modules, "zerg.main", fake_main)

    fake_uvicorn = ModuleType("uvicorn")
    fake_uvicorn.run = lambda *args, **kwargs: uvicorn_calls.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    monkeypatch.setattr(config_file_cli, "load_config", lambda: config)
    monkeypatch.setattr(
        config_file_cli,
        "save_loaded_config",
        lambda value, config_path=None, claude_dir=None: saved_configs.append(config_file_cli.config_to_dict(value)),
    )

    return uvicorn_calls, saved_configs


def test_serve_uses_saved_public_url_for_runtime_env(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)

    uvicorn_calls, saved_configs = _patch_serve(
        monkeypatch,
        tmp_path,
        config=_base_config(public_url="https://saved.example.com"),
    )

    serve_cli.serve(
        host="0.0.0.0",
        port=8080,
        reload=False,
        db=None,
        workers=1,
        daemon=False,
        stop=False,
        demo=False,
        demo_fresh=False,
        domain=None,
        allow_public_no_auth=True,
    )

    assert os.environ["APP_PUBLIC_URL"] == "https://saved.example.com"
    assert os.environ["PUBLIC_SITE_URL"] == "https://saved.example.com"
    assert saved_configs == []
    assert uvicorn_calls[0][1]["host"] == "0.0.0.0"
    assert uvicorn_calls[0][1]["ws_ping_interval"] is None


def test_serve_keeps_explicit_runtime_public_url_when_no_domain_passed(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://env.example.com")
    monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)

    _patch_serve(
        monkeypatch,
        tmp_path,
        config=_base_config(public_url="https://saved.example.com"),
    )

    serve_cli.serve(
        host="0.0.0.0",
        port=8080,
        reload=False,
        db=None,
        workers=1,
        daemon=False,
        stop=False,
        demo=False,
        demo_fresh=False,
        domain=None,
        allow_public_no_auth=True,
    )

    assert os.environ["APP_PUBLIC_URL"] == "https://env.example.com"
    assert os.environ["PUBLIC_SITE_URL"] == "https://env.example.com"


def test_serve_domain_overrides_runtime_env_and_persists_config(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://env.example.com")
    monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)

    uvicorn_calls, saved_configs = _patch_serve(
        monkeypatch,
        tmp_path,
        config=_base_config(public_url="https://saved.example.com"),
    )

    serve_cli.serve(
        host="0.0.0.0",
        port=8080,
        reload=False,
        db=None,
        workers=1,
        daemon=False,
        stop=False,
        demo=False,
        demo_fresh=False,
        domain="longhouse.example.com",
        allow_public_no_auth=True,
    )

    assert os.environ["APP_PUBLIC_URL"] == "https://longhouse.example.com"
    assert os.environ["PUBLIC_SITE_URL"] == "https://longhouse.example.com"
    assert saved_configs[0]["server"]["public_url"] == "https://longhouse.example.com"
    assert saved_configs[0]["shipper"] == {
        "fallback_scan_secs": 300,
    }
    assert uvicorn_calls[0][1]["host"] == "0.0.0.0"
