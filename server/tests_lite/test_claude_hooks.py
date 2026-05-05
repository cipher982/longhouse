"""Tests for Claude hook installation and session binding behavior."""

from zerg.services.shipper.hooks import HOOK_SCRIPT
from zerg.services.shipper.hooks import _make_hook_entries


def test_claude_hook_seeds_session_binding_on_stop():
    """Hook seeds session_binding via engine bind instead of shipping directly."""
    assert "__ENGINE_PATH__" in HOOK_SCRIPT
    assert 'bind --path "$TRANSCRIPT" --session-id "$MANAGED_SESSION_ID"' in HOOK_SCRIPT
    # No direct shipping — daemon handles it
    assert "ship --file" not in HOOK_SCRIPT
    assert "nohup" not in HOOK_SCRIPT


def test_claude_hook_writes_presence_to_outbox():
    assert 'LONGHOUSE_HOME="${LONGHOUSE_HOME:-__LONGHOUSE_HOME__}"' in HOOK_SCRIPT
    assert 'OUTBOX="$LONGHOUSE_HOME/agent/outbox"' in HOOK_SCRIPT
    assert "transcript_path: $transcript" in HOOK_SCRIPT
    assert "find_provider_pid()" in HOOK_SCRIPT
    assert "control_path: $control_path" in HOOK_SCRIPT
    assert "provider_pid" in HOOK_SCRIPT
    assert 'write_presence_outbox "$PAYLOAD" >/dev/null 2>&1 || true' in HOOK_SCRIPT


def test_claude_hook_does_not_inject_startup_context_by_default():
    # Startup continuity injection lives in labs/startup-continuity, not the
    # default install. The default hook must stay observation-only.
    assert "/api/agents/sessions/startup-context" not in HOOK_SCRIPT
    assert "LONGHOUSE_HOOK_URL" not in HOOK_SCRIPT
    assert "LONGHOUSE_HOOK_TOKEN" not in HOOK_SCRIPT
    assert "hookSpecificOutput" not in HOOK_SCRIPT


def test_claude_hook_hot_path_stays_local_only():
    assert 'PRESENCE_MODE="${LONGHOUSE_HOOK_PRESENCE_MODE:-auto}"' not in HOOK_SCRIPT
    assert "/api/agents/presence" not in HOOK_SCRIPT
    assert "emit_presence()" not in HOOK_SCRIPT
    assert "LONGHOUSE_MANAGED_SESSION_ID" in HOOK_SCRIPT
    assert "write_presence_outbox()" in HOOK_SCRIPT


def test_claude_stop_hook_forces_sidechain_for_hindsight_workspace():
    assert 'FORCE_SIDECHAIN="${LONGHOUSE_IS_SIDECHAIN:-0}"' in HOOK_SCRIPT
    assert 'HINDSIGHT_ROOT="__HINDSIGHT_ROOT__"' in HOOK_SCRIPT
    assert 'case "$CWD" in' in HOOK_SCRIPT


def test_claude_stop_hook_entry_is_sync(tmp_path):
    """Stop hook is now sync (no shipping, just outbox write + binding seed)."""
    stop_entry, _lifecycle_entry = _make_hook_entries(tmp_path)
    hook = stop_entry["hooks"][0]
    assert hook["async"] is False
    assert hook["timeout"] == 5
