"""Tests for Codex hook installation and hooks.json management."""

import json
import stat
from pathlib import Path

from zerg.services.shipper.hooks import (
    CODEX_HOOK_SCRIPT,
    install_codex_hooks,
    _is_longhouse_codex_hook,
    _merge_codex_hooks_for_event,
)


def test_codex_hook_script_template_has_required_markers():
    """Hook script must contain the key patterns the engine + outbox expect."""
    assert "hook_event_name" in CODEX_HOOK_SCRIPT, "must read Codex snake_case hook event input"
    assert "session_id" in CODEX_HOOK_SCRIPT, "must read Codex session ID"
    assert "transcript_path" in CODEX_HOOK_SCRIPT, "must read transcript path"
    assert "prs." in CODEX_HOOK_SCRIPT, "must use prs.*.json outbox naming"
    assert ".tmp." in CODEX_HOOK_SCRIPT, "must use atomic tmp write pattern"
    assert "__ENGINE_PATH__" in CODEX_HOOK_SCRIPT, "must have engine path placeholder"
    # The template uses __ENGINE_PATH__ as a placeholder; the literal string
    # "longhouse-engine" appears in comments but the actual command line must
    # use the placeholder so install_codex_hooks can bake in the real path.
    assert 'ENGINE="__ENGINE_PATH__"' in CODEX_HOOK_SCRIPT, "must use placeholder in the command variable"
    assert 'provider: $provider' in CODEX_HOOK_SCRIPT, "must include provider in presence payload"


def test_codex_hook_script_has_managed_session_id_support():
    """Hook script must have explicit managed vs unmanaged session ID paths."""
    assert "LONGHOUSE_MANAGED_SESSION_ID" in CODEX_HOOK_SCRIPT, "must check for managed-session env"
    assert "CODEX_SESSION_ID" in CODEX_HOOK_SCRIPT, "must read Codex's native session ID"
    # Managed path: uses launcher-injected managed session env for outbox and transcript ship.
    assert "--session-id" in CODEX_HOOK_SCRIPT, "must pass --session-id override to engine for managed sessions"
    # No fallback pattern — two explicit paths
    assert "SID=" in CODEX_HOOK_SCRIPT, "must assign SID explicitly in each path"
    assert '--arg provider "codex"' in CODEX_HOOK_SCRIPT, "must stamp Codex presence events with provider=codex"


def test_codex_hook_script_supports_direct_hook_target_overrides():
    assert 'TARGET_URL="${LONGHOUSE_HOOK_URL:-}"' in CODEX_HOOK_SCRIPT
    assert 'TARGET_TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"' in CODEX_HOOK_SCRIPT
    assert 'X-Agents-Token: $TARGET_TOKEN' in CODEX_HOOK_SCRIPT
    assert '${TARGET_URL%/}/api/agents/presence' in CODEX_HOOK_SCRIPT


def test_codex_hook_script_maps_all_events():
    """Hook script must handle all three Codex hook events."""
    assert "SessionStart)" in CODEX_HOOK_SCRIPT
    assert "UserPromptSubmit)" in CODEX_HOOK_SCRIPT
    assert "Stop)" in CODEX_HOOK_SCRIPT


def test_is_longhouse_codex_hook_identifies_our_hooks():
    our_hook = {"hooks": [{"type": "command", "command": "/home/user/.codex/hooks/longhouse-codex-hook.sh"}]}
    assert _is_longhouse_codex_hook(our_hook) is True


def test_is_longhouse_codex_hook_ignores_other_hooks():
    other_hook = {"hooks": [{"type": "command", "command": "/usr/local/bin/my-custom-hook.sh"}]}
    assert _is_longhouse_codex_hook(other_hook) is False


def test_merge_codex_hooks_appends_when_empty():
    new = {"hooks": [{"type": "command", "command": "longhouse-codex-hook.sh"}]}
    result = _merge_codex_hooks_for_event([], new)
    assert len(result) == 1
    assert result[0] == new


def test_merge_codex_hooks_replaces_existing():
    old = {"hooks": [{"type": "command", "command": "longhouse-old.sh"}]}
    new = {"hooks": [{"type": "command", "command": "longhouse-new.sh"}]}
    result = _merge_codex_hooks_for_event([old], new)
    assert len(result) == 1
    assert result[0] == new


def test_merge_codex_hooks_preserves_user_hooks():
    user = {"hooks": [{"type": "command", "command": "/usr/local/bin/custom.sh"}]}
    ours = {"hooks": [{"type": "command", "command": "longhouse-codex-hook.sh"}]}
    result = _merge_codex_hooks_for_event([user], ours)
    assert len(result) == 2
    assert result[0] == user
    assert result[1] == ours


def test_install_codex_hooks_skips_when_no_codex_dir(tmp_path, monkeypatch):
    """Should return empty list when ~/.codex doesn't exist."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    actions = install_codex_hooks(engine_path="/usr/bin/longhouse-engine")
    assert actions == []


def test_install_codex_hooks_creates_hook_script(tmp_path, monkeypatch):
    """Should create the hook script and hooks.json when ~/.codex exists."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    actions = install_codex_hooks(engine_path="/usr/bin/longhouse-engine")
    assert len(actions) == 2

    hook_script = codex_dir / "hooks" / "longhouse-codex-hook.sh"
    assert hook_script.exists()
    assert hook_script.stat().st_mode & stat.S_IXUSR

    content = hook_script.read_text()
    assert "/usr/bin/longhouse-engine" in content
    assert "hook_event_name" in content


def test_install_codex_hooks_creates_valid_hooks_json(tmp_path, monkeypatch):
    """hooks.json must have the right structure for Codex to parse."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    install_codex_hooks(engine_path="/usr/bin/longhouse-engine")

    hooks_json = codex_dir / "hooks.json"
    assert hooks_json.exists()

    data = json.loads(hooks_json.read_text())
    hooks = data["hooks"]

    # Must have all three Codex hook events (PascalCase keys)
    assert "SessionStart" in hooks
    assert "UserPromptSubmit" in hooks
    assert "Stop" in hooks

    # Each event should be an array of MatcherGroups
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        groups = hooks[event]
        assert isinstance(groups, list)
        assert len(groups) == 1
        group = groups[0]
        assert "hooks" in group
        assert len(group["hooks"]) == 1
        handler = group["hooks"][0]
        assert handler["type"] == "command"
        assert "longhouse-codex-hook.sh" in handler["command"]

    # All hooks use 5s timeout (no shipping, just outbox write + binding)
    assert hooks["Stop"][0]["hooks"][0]["timeout"] == 5
    assert hooks["SessionStart"][0]["hooks"][0]["timeout"] == 5


def test_install_codex_hooks_preserves_existing_hooks_json(tmp_path, monkeypatch):
    """Should preserve user's existing hooks when adding Longhouse hooks."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    # Pre-existing hooks.json with a user hook
    existing = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "/usr/local/bin/my-hook.sh"}]}
            ]
        }
    }
    hooks_json = codex_dir / "hooks.json"
    hooks_json.write_text(json.dumps(existing))

    install_codex_hooks(engine_path="/usr/bin/longhouse-engine")

    data = json.loads(hooks_json.read_text())
    session_start = data["hooks"]["SessionStart"]

    # Should have both: user hook preserved, Longhouse hook added
    assert len(session_start) == 2
    assert "/usr/local/bin/my-hook.sh" in session_start[0]["hooks"][0]["command"]
    assert "longhouse-codex-hook.sh" in session_start[1]["hooks"][0]["command"]


def test_install_codex_hooks_is_idempotent(tmp_path, monkeypatch):
    """Running install twice should not duplicate hooks."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    install_codex_hooks(engine_path="/usr/bin/longhouse-engine")
    install_codex_hooks(engine_path="/usr/bin/longhouse-engine")

    data = json.loads((codex_dir / "hooks.json").read_text())

    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        assert len(data["hooks"][event]) == 1, f"{event} should have exactly 1 entry after double install"


def test_install_codex_hooks_handles_corrupt_hooks_json(tmp_path, monkeypatch):
    """Should recover from corrupt hooks.json."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    hooks_json = codex_dir / "hooks.json"
    hooks_json.write_text("not valid json!!!")

    actions = install_codex_hooks(engine_path="/usr/bin/longhouse-engine")
    assert len(actions) == 2

    # Should have written a valid hooks.json
    data = json.loads(hooks_json.read_text())
    assert "hooks" in data
    assert "Stop" in data["hooks"]


def test_install_hooks_also_installs_codex(tmp_path, monkeypatch):
    """install_hooks() should also install Codex hooks when ~/.codex exists."""
    from zerg.services.shipper.hooks import install_hooks

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    actions = install_hooks(
        url="http://localhost:8080",
        claude_dir=str(claude_dir),
        engine_path="/usr/bin/longhouse-engine",
    )

    # Should include both Claude and Codex actions
    codex_actions = [a for a in actions if "codex" in a.lower() or "Codex" in a]
    assert len(codex_actions) >= 1, f"Expected Codex actions, got: {actions}"

    # Codex hooks.json should exist
    assert (codex_dir / "hooks.json").exists()


def test_install_hooks_skips_codex_when_not_installed(tmp_path, monkeypatch):
    """install_hooks() should not fail when ~/.codex doesn't exist."""
    from zerg.services.shipper.hooks import install_hooks

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)

    # No ~/.codex directory
    actions = install_hooks(
        url="http://localhost:8080",
        claude_dir=str(claude_dir),
        engine_path="/usr/bin/longhouse-engine",
    )

    # Should still succeed with just Claude hooks
    assert any("settings.json" in a for a in actions)
    # No Codex hooks.json should have been created
    codex_hooks_json = tmp_path / ".codex" / "hooks.json"
    assert not codex_hooks_json.exists()
