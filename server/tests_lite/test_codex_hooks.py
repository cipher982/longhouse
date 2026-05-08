"""Tests for Codex hook installation and hooks.json management."""

import json
import os
import stat

from zerg.services.shipper.hooks import CODEX_HOOK_SCRIPT
from zerg.services.shipper.hooks import _is_longhouse_codex_hook
from zerg.services.shipper.hooks import _merge_codex_hooks_for_event
from zerg.services.shipper.hooks import install_codex_hooks
from zerg.services.shipper.hooks import install_hooks


def test_codex_hook_script_template_has_required_markers():
    """Hook script must contain the key patterns the engine + outbox expect."""
    assert "hook_event_name" in CODEX_HOOK_SCRIPT, "must read Codex snake_case hook event input"
    assert "session_id" in CODEX_HOOK_SCRIPT, "must read Codex session ID"
    assert "tool_name" in CODEX_HOOK_SCRIPT, "must read Codex tool hook names"
    assert "transcript_path" in CODEX_HOOK_SCRIPT, "must read transcript path"
    assert 'LONGHOUSE_HOME="${LONGHOUSE_HOME:-__LONGHOUSE_HOME__}"' in CODEX_HOOK_SCRIPT
    assert "prs." in CODEX_HOOK_SCRIPT, "must use prs.*.json outbox naming"
    assert ".tmp." in CODEX_HOOK_SCRIPT, "must use atomic tmp write pattern"
    assert "__ENGINE_PATH__" in CODEX_HOOK_SCRIPT, "must have engine path placeholder"
    # The template uses __ENGINE_PATH__ as a placeholder; the literal string
    # "longhouse-engine" appears in comments but the actual command line must
    # use the placeholder so install_codex_hooks can bake in the real path.
    assert 'ENGINE="__ENGINE_PATH__"' in CODEX_HOOK_SCRIPT, "must use placeholder in the command variable"
    assert "provider: $provider" in CODEX_HOOK_SCRIPT, "must include provider in presence payload"
    assert "tool_name: $tool" in CODEX_HOOK_SCRIPT, "must include tool names in presence payload"
    assert "transcript_path: $transcript" in CODEX_HOOK_SCRIPT, "must include transcript path in presence payload"


def test_codex_hook_script_has_managed_session_id_support():
    """Hook script must have explicit managed vs unmanaged session ID paths."""
    assert "LONGHOUSE_MANAGED_SESSION_ID" in CODEX_HOOK_SCRIPT, "must check for managed-session env"
    assert "CODEX_SESSION_ID" in CODEX_HOOK_SCRIPT, "must read Codex's native session ID"
    # Managed path: uses launcher-injected managed session env for outbox and transcript ship.
    assert "--session-id" in CODEX_HOOK_SCRIPT, "must pass --session-id override to engine for managed sessions"
    # No fallback pattern — two explicit paths
    assert "SID=" in CODEX_HOOK_SCRIPT, "must assign SID explicitly in each path"
    assert '--arg provider "codex"' in CODEX_HOOK_SCRIPT, "must stamp Codex presence events with provider=codex"


def test_codex_hook_does_not_inject_startup_context_by_default():
    # Startup continuity injection lives in labs/startup-continuity, not the
    # default install. The default hook must stay observation-only.
    assert '/api/agents/sessions/startup-context' not in CODEX_HOOK_SCRIPT
    assert 'LONGHOUSE_HOOK_URL' not in CODEX_HOOK_SCRIPT
    assert 'LONGHOUSE_HOOK_TOKEN' not in CODEX_HOOK_SCRIPT
    assert 'hookSpecificOutput' not in CODEX_HOOK_SCRIPT


def test_codex_hook_hot_path_stays_local_only():
    assert 'PRESENCE_MODE="${LONGHOUSE_HOOK_PRESENCE_MODE:-auto}"' not in CODEX_HOOK_SCRIPT
    assert "/api/agents/presence" not in CODEX_HOOK_SCRIPT
    assert "emit_presence()" not in CODEX_HOOK_SCRIPT
    assert "write_presence_outbox()" in CODEX_HOOK_SCRIPT
    assert 'write_presence_outbox "$PAYLOAD" >/dev/null 2>&1 || true' in CODEX_HOOK_SCRIPT


def test_codex_hook_script_maps_all_events():
    """Hook script must handle the Codex hook events that produce liveness facts."""
    assert "SessionStart)" in CODEX_HOOK_SCRIPT
    assert "UserPromptSubmit)" in CODEX_HOOK_SCRIPT
    assert "PreToolUse)" in CODEX_HOOK_SCRIPT
    assert "PostToolUse)" in CODEX_HOOK_SCRIPT
    assert "PermissionRequest)" in CODEX_HOOK_SCRIPT
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
    assert str(tmp_path / ".longhouse") in content
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

    # Must have all Codex hook events that map to explicit liveness facts.
    expected_events = {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "Stop",
    }
    assert expected_events.issubset(hooks)

    # Each event should be an array of MatcherGroups
    for event in expected_events:
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
    for event in expected_events:
        assert hooks[event][0]["hooks"][0]["timeout"] == 5


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

    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "Stop",
    ):
        assert len(data["hooks"][event]) == 1, (
            f"{event} should have exactly 1 entry after double install"
        )


def test_install_codex_hooks_does_not_rewrite_unchanged_files(tmp_path, monkeypatch):
    """Running install twice should preserve mtimes when hook files are unchanged."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    install_codex_hooks(engine_path="/usr/bin/longhouse-engine")
    hook_script = codex_dir / "hooks" / "longhouse-codex-hook.sh"
    hooks_json = codex_dir / "hooks.json"

    old_ns = 1_700_000_000_000_000_000
    os.utime(hook_script, ns=(old_ns, old_ns))
    os.utime(hooks_json, ns=(old_ns, old_ns))

    actions = install_codex_hooks(engine_path="/usr/bin/longhouse-engine")

    assert actions == [
        f"{hook_script} already up to date",
        f"{hooks_json} already up to date",
    ]
    assert hook_script.stat().st_mtime_ns == old_ns
    assert hooks_json.stat().st_mtime_ns == old_ns


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


def test_install_hooks_replaces_deprecated_claude_session_start_hook_with_unified_hook(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)

    session_start_script = hooks_dir / "longhouse-session-start.sh"
    session_start_script.write_text("#!/bin/bash\nexit 0\n")

    settings_json = claude_dir / "settings.json"
    settings_json.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": str(session_start_script), "async": False, "timeout": 5}]},
                        {"hooks": [{"type": "command", "command": "/usr/local/bin/custom-session-start.sh", "async": False, "timeout": 5}]},
                    ]
                }
            }
        )
    )

    install_hooks(
        url="http://localhost:8080",
        claude_dir=str(claude_dir),
        engine_path="/usr/bin/longhouse-engine",
    )

    assert not session_start_script.exists()

    data = json.loads(settings_json.read_text())
    session_start = data["hooks"]["SessionStart"]
    assert len(session_start) == 2
    commands = [entry["hooks"][0]["command"] for entry in session_start]
    assert "/usr/local/bin/custom-session-start.sh" in commands
    assert str(hooks_dir / "longhouse-hook.sh") in commands


def test_install_hooks_points_lifecycle_hooks_at_longhouse_agent_state(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    install_hooks(
        url="http://localhost:8080",
        claude_dir=str(claude_dir),
        engine_path="/usr/bin/longhouse-engine",
    )

    claude_hook = (claude_dir / "hooks" / "longhouse-hook.sh").read_text()
    codex_hook = (codex_dir / "hooks" / "longhouse-codex-hook.sh")
    expected_home = str(tmp_path / ".longhouse")

    assert expected_home in claude_hook
    assert f'{claude_dir / "hindsight"}' in claude_hook
    assert "$LONGHOUSE_HOME/agent/outbox" in claude_hook

    codex_content = codex_hook.read_text()
    assert expected_home in codex_content
    assert "$LONGHOUSE_HOME/agent/outbox" in codex_content


def test_install_hooks_ensures_claude_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)

    actions = install_hooks(
        url="http://localhost:8080",
        claude_dir=str(claude_dir),
        engine_path="/usr/bin/longhouse-engine",
    )

    assert (claude_dir / "projects").is_dir()
    assert any(str(claude_dir / "projects") in action for action in actions)
