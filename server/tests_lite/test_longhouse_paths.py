from __future__ import annotations

from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_log_dir
from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.longhouse_paths import get_agent_status_path
from zerg.services.longhouse_paths import get_legacy_claude_managed_local_dir
from zerg.services.longhouse_paths import is_stable_longhouse_home
from zerg.services.longhouse_paths import resolve_longhouse_home
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home


def test_resolve_longhouse_home_maps_provider_dir_to_sibling_longhouse(tmp_path):
    assert resolve_longhouse_home(tmp_path / ".claude") == tmp_path / ".longhouse"


def test_resolve_longhouse_home_preserves_explicit_longhouse_home(tmp_path):
    assert resolve_longhouse_home(tmp_path) == tmp_path


def test_resolve_longhouse_home_uses_claude_env_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    assert resolve_longhouse_home() == tmp_path / ".longhouse"


def test_resolve_longhouse_home_prefers_longhouse_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / "custom-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    assert resolve_longhouse_home() == tmp_path / "custom-home"


def test_resolve_longhouse_home_maps_custom_provider_env_to_sibling_longhouse(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))

    assert resolve_longhouse_home() == tmp_path / ".longhouse"


def test_resolve_longhouse_home_from_provider_home_maps_custom_provider_path(tmp_path):
    assert resolve_longhouse_home_from_provider_home(tmp_path / "claude-config") == tmp_path / ".longhouse"


def test_is_stable_longhouse_home_tracks_longhouse_home_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    scratch_home = home / ".longhouse-dev"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LONGHOUSE_HOME", str(scratch_home))

    assert is_stable_longhouse_home() is False
    assert is_stable_longhouse_home(scratch_home) is False
    assert is_stable_longhouse_home(home / ".longhouse") is True


def test_agent_state_paths_live_under_agent_dir(tmp_path):
    assert get_agent_outbox_dir(tmp_path) == tmp_path / "agent" / "outbox"
    assert get_agent_status_path(tmp_path) == tmp_path / "agent" / "engine-status.json"
    assert get_agent_db_path(tmp_path) == tmp_path / "agent" / "longhouse-shipper.db"
    assert get_agent_log_dir(tmp_path) == tmp_path / "agent" / "logs"


def test_legacy_claude_managed_local_prefers_claude_config_dir(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))

    assert get_legacy_claude_managed_local_dir("codex-bridge") == (claude_home / "managed-local" / "codex-bridge")


def test_legacy_claude_managed_local_uses_sibling_for_longhouse_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert get_legacy_claude_managed_local_dir("opencode", base_dir=tmp_path / ".longhouse") == (
        tmp_path / ".claude" / "managed-local" / "opencode"
    )


def test_legacy_claude_managed_local_uses_real_claude_home_for_non_default_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert get_legacy_claude_managed_local_dir("antigravity", base_dir=tmp_path / "lh-state") == (
        home / ".claude" / "managed-local" / "antigravity"
    )
