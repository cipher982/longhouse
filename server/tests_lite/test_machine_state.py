from __future__ import annotations

import json
from pathlib import Path

from zerg.services.machine_state import clear_machine_runtime_url
from zerg.services.machine_state import machine_state_source_hash
from zerg.services.machine_state import read_machine_state
from zerg.services.machine_state import write_machine_state


def test_write_machine_state_updates_single_authoritative_record(tmp_path: Path):
    first = write_machine_state(
        base_dir=tmp_path,
        written_by="connect-install",
        runtime_url="https://demo.longhouse.test",
        machine_name="test box",
        topology_intent="connect-remote",
        desktop_app_enabled=True,
        runner_enabled=True,
    )
    second = write_machine_state(
        base_dir=tmp_path,
        written_by="auth",
        runtime_url="https://prod.longhouse.test",
    )

    assert first.runtime_url == "https://demo.longhouse.test"
    assert first.machine_name == "test-box"
    assert second.runtime_url == "https://prod.longhouse.test"
    assert second.machine_name == "test-box"
    assert second.topology_intent == "connect-remote"
    assert second.desktop_app_enabled is True
    assert second.runner_enabled is True

    state_path, loaded, error = read_machine_state(tmp_path)
    assert error is None
    assert state_path == tmp_path / "machine" / "state.json"
    assert loaded == second

    journal_path = tmp_path / "machine" / "state-journal.jsonl"
    entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert len(entries) == 2
    assert entries[0]["written_by"] == "connect-install"
    assert entries[1]["written_by"] == "auth"
    assert entries[1]["old"]["runtime_url"] == "https://demo.longhouse.test"
    assert entries[1]["new"]["runtime_url"] == "https://prod.longhouse.test"


def test_clear_machine_runtime_url_preserves_other_machine_state(tmp_path: Path):
    write_machine_state(
        base_dir=tmp_path,
        written_by="connect-install",
        runtime_url="https://demo.longhouse.test",
        machine_name="test-box",
        topology_intent="connect-remote",
    )

    assert clear_machine_runtime_url(tmp_path, written_by="auth-clear") is True

    _state_path, loaded, error = read_machine_state(tmp_path)
    assert error is None
    assert loaded is not None
    assert loaded.runtime_url is None
    assert loaded.machine_name == "test-box"
    assert loaded.topology_intent == "connect-remote"
    assert loaded.written_by == "auth-clear"


def test_write_machine_state_preserves_generation_when_launch_config_is_unchanged(tmp_path: Path):
    first = write_machine_state(
        base_dir=tmp_path,
        written_by="connect-install",
        runtime_url="https://demo.longhouse.test",
        machine_name="test-box",
        topology_intent="connect-remote",
        desktop_app_enabled=True,
    )

    second = write_machine_state(
        base_dir=tmp_path,
        written_by="auth",
        runtime_url="https://demo.longhouse.test",
    )

    assert second.runtime_url == first.runtime_url
    assert second.machine_name == first.machine_name
    assert second.config_generation == first.config_generation
    assert second.written_by == "auth"


def test_machine_state_generation_ignores_legacy_topology_and_runner_flags(tmp_path: Path):
    first = write_machine_state(
        base_dir=tmp_path,
        written_by="connect-install",
        runtime_url="https://demo.longhouse.test",
        machine_name="test-box",
        topology_intent="connect-remote",
        runner_enabled=True,
        desktop_app_enabled=True,
    )

    second = write_machine_state(
        base_dir=tmp_path,
        written_by="machine-configure",
        topology_intent="serve-local",
        runner_enabled=False,
    )

    assert second.topology_intent == "serve-local"
    assert second.runner_enabled is False
    assert second.config_generation == first.config_generation
    assert machine_state_source_hash(second) == machine_state_source_hash(first)
