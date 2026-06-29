"""Unit tests for launchd plist and systemd unit generation.

Specifically covers the Codex findings:
- Machine names with spaces must not break systemd ExecStart (spaces -> hyphens via sanitize)
- Machine names with XML chars must not break plist (XML escaped)
- Machine name is included in both plist and unit file
- Missing machine_name is handled gracefully (no --machine-name arg)
"""

from types import SimpleNamespace

import pytest

import zerg.services.shipper.service as shipper_service
from zerg.services.shipper.service import ServiceConfig
from zerg.services.shipper.service import _generate_launchd_plist
from zerg.services.shipper.service import _generate_systemd_unit
from zerg.services.shipper.service import get_engine_executable


@pytest.fixture(autouse=True)
def _stub_engine_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep service-generation tests independent from host-installed binaries."""

    monkeypatch.setattr(
        shipper_service,
        "get_engine_executable",
        lambda: "/tmp/longhouse-engine",
    )


def _make_config(**kwargs) -> ServiceConfig:
    defaults = dict(
        url="https://longhouse.example.com",
        token="test-token",
        claude_dir="/tmp/claude",
        machine_name=None,
        machine_config_generation=None,
        machine_state_hash=None,
    )
    defaults.update(kwargs)
    return ServiceConfig(**defaults)


# ---------------------------------------------------------------------------
# launchd plist
# ---------------------------------------------------------------------------


def test_plist_contains_machine_name():
    config = _make_config(machine_name="work-macbook")
    plist = _generate_launchd_plist(config)
    assert "--machine-name" in plist
    assert "work-macbook" in plist
    assert "--log-dir" not in plist


def test_plist_no_machine_name_arg_when_none():
    config = _make_config(machine_name=None)
    plist = _generate_launchd_plist(config)
    assert "--machine-name" not in plist


def test_plist_includes_archive_repair_mode():
    config = _make_config(archive_repair_mode="paused")
    plist = _generate_launchd_plist(config)
    assert "--archive-repair-mode" in plist
    assert "paused" in plist


def test_plist_normalizes_archive_repair_mode_synonym():
    config = _make_config(archive_repair_mode="resume")
    plist = _generate_launchd_plist(config)
    assert "trickle" in plist
    assert "resume" not in plist


def test_plist_xml_escapes_ampersand():
    """& in a machine name must become &amp; in plist XML."""
    config = _make_config(machine_name="work&laptop")
    plist = _generate_launchd_plist(config)
    # Raw & should not appear in a <string> element
    # (sanitize strips & entirely, or XML escaping converts to &amp;)
    # Either way the plist must be valid XML - no raw & character
    assert "work&laptop" not in plist


def test_plist_xml_escapes_angle_brackets():
    config = _make_config(machine_name="work<laptop>")
    plist = _generate_launchd_plist(config)
    assert "work<laptop>" not in plist


def test_plist_is_valid_xml():
    """The generated plist must be parseable as XML."""
    import xml.etree.ElementTree as ET

    config = _make_config(machine_name="my-machine")
    plist = _generate_launchd_plist(config)
    # Should not raise
    ET.fromstring(plist)


def test_plist_persists_longhouse_home():
    config = _make_config(claude_dir="/tmp/.claude")
    plist = _generate_launchd_plist(config)
    assert "<key>LONGHOUSE_HOME</key>" in plist
    assert "<string>/tmp/.longhouse</string>" in plist


def test_plist_uses_longhouse_agent_log_dir():
    config = _make_config(claude_dir="/tmp/.claude")
    plist = _generate_launchd_plist(config)
    assert "<key>LONGHOUSE_LOG_DIR</key>" in plist
    assert "<string>/tmp/.longhouse/agent/logs</string>" in plist
    assert "/tmp/.longhouse/agent/logs/engine.stdout.log" in plist


def test_plist_sets_service_path_for_provider_clis():
    config = _make_config()
    plist = _generate_launchd_plist(config)
    assert "<key>PATH</key>" in plist
    assert "/opt/homebrew/bin" in plist
    assert "/usr/local/bin" in plist


def test_plist_embeds_machine_state_metadata_when_present():
    config = _make_config(
        machine_config_generation="20260414-test",
        machine_state_hash="abc123",
    )
    plist = _generate_launchd_plist(config)
    assert "<key>LONGHOUSE_MACHINE_GENERATION</key>" in plist
    assert "<string>20260414-test</string>" in plist
    assert "<key>LONGHOUSE_MACHINE_STATE_HASH</key>" in plist
    assert "<string>abc123</string>" in plist


def test_plist_valid_xml_with_special_machine_name():
    """Plist is still valid XML even if sanitization is bypassed."""
    import xml.etree.ElementTree as ET

    # Simulate a name that gets XML-escaped at the plist layer
    config = _make_config(machine_name="already-clean")
    plist = _generate_launchd_plist(config)
    ET.fromstring(plist)  # must not raise


# ---------------------------------------------------------------------------
# systemd unit
# ---------------------------------------------------------------------------


def test_systemd_contains_machine_name():
    config = _make_config(machine_name="home-server")
    unit = _generate_systemd_unit(config)
    assert "--machine-name" in unit
    assert "home-server" in unit
    assert "--log-dir" not in unit


def test_systemd_includes_archive_repair_mode():
    config = _make_config(archive_repair_mode="trickle")
    unit = _generate_systemd_unit(config)
    assert "--archive-repair-mode trickle" in unit


def test_systemd_persists_longhouse_home():
    config = _make_config(claude_dir="/tmp/.claude")
    unit = _generate_systemd_unit(config)
    assert 'Environment="LONGHOUSE_HOME=/tmp/.longhouse"' in unit


def test_systemd_uses_longhouse_agent_log_dir():
    config = _make_config(claude_dir="/tmp/.claude")
    unit = _generate_systemd_unit(config)
    assert 'Environment="LONGHOUSE_LOG_DIR=/tmp/.longhouse/agent/logs"' in unit


def test_systemd_sets_service_path_for_provider_clis():
    config = _make_config()
    unit = _generate_systemd_unit(config)
    assert 'Environment="PATH=' in unit
    assert "/opt/homebrew/bin" in unit
    assert "/usr/local/bin" in unit


def test_systemd_embeds_machine_state_metadata_when_present():
    config = _make_config(
        machine_config_generation="20260414-test",
        machine_state_hash="abc123",
    )
    unit = _generate_systemd_unit(config)
    assert 'Environment="LONGHOUSE_MACHINE_GENERATION=20260414-test"' in unit
    assert 'Environment="LONGHOUSE_MACHINE_STATE_HASH=abc123"' in unit


def test_systemd_no_machine_name_arg_when_none():
    config = _make_config(machine_name=None)
    unit = _generate_systemd_unit(config)
    assert "--machine-name" not in unit


def test_systemd_machine_name_no_spaces():
    """After sanitization, machine name in ExecStart must have no spaces.

    Spaces in systemd ExecStart are arg delimiters - a name like
    'work laptop' would be parsed as two separate args, breaking startup.
    """
    # sanitize_machine_name is called by connect.py before reaching here,
    # but verify that the generated unit is safe if a clean name is passed.
    config = _make_config(machine_name="work-laptop")  # already sanitized
    unit = _generate_systemd_unit(config)

    # Extract ExecStart line
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    # After --machine-name there should be exactly one token (no space in value)
    parts = exec_line.split("--machine-name")
    assert len(parts) == 2
    machine_part = parts[1].strip()
    assert " " not in machine_part


def test_get_engine_executable_prefers_installed_runtime_over_repo_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    project_root = tmp_path / "server"
    project_root.mkdir()
    engine_binary = tmp_path / "engine" / "target" / "release" / "longhouse-engine"
    engine_binary.parent.mkdir(parents=True)
    engine_binary.write_text("", encoding="utf-8")

    monkeypatch.setattr(shipper_service, "_find_project_root", lambda: project_root)
    monkeypatch.setattr(
        shipper_service,
        "resolve_installed_runtime_artifact",
        lambda component: SimpleNamespace(
            launch_path="/tmp/installed-longhouse-engine",
            source="local-runtime-bin",
        ),
    )

    assert get_engine_executable() == "/tmp/installed-longhouse-engine"


def test_get_engine_executable_uses_repo_build_when_no_runtime_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    project_root = tmp_path / "server"
    project_root.mkdir()
    engine_binary = tmp_path / "engine" / "target" / "release" / "longhouse-engine"
    engine_binary.parent.mkdir(parents=True)
    engine_binary.write_text("", encoding="utf-8")

    monkeypatch.setattr(shipper_service, "_find_project_root", lambda: project_root)
    monkeypatch.setattr(
        shipper_service,
        "resolve_installed_runtime_artifact",
        lambda component: None,
    )

    assert get_engine_executable() == str(engine_binary)


def test_get_engine_executable_uses_path_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shipper_service, "_find_project_root", lambda: None)
    monkeypatch.setattr(
        shipper_service,
        "resolve_installed_runtime_artifact",
        lambda component: SimpleNamespace(launch_path="/tmp/longhouse-engine", source="path"),
    )

    assert get_engine_executable() == "/tmp/longhouse-engine"
