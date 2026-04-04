"""Tests for Claude hook installation and session binding behavior."""

from zerg.services.shipper.hooks import HOOK_SCRIPT
from zerg.services.shipper.hooks import SESSION_START_HOOK_SCRIPT
from zerg.services.shipper.hooks import _make_hook_entries


def test_claude_hook_seeds_session_binding_on_stop():
    """Hook seeds session_binding via engine bind instead of shipping directly."""
    assert '__ENGINE_PATH__' in HOOK_SCRIPT
    assert 'bind --path "$TRANSCRIPT" --session-id "$MANAGED_SESSION_ID"' in HOOK_SCRIPT
    # No direct shipping — daemon handles it
    assert 'ship --file' not in HOOK_SCRIPT
    assert 'nohup' not in HOOK_SCRIPT


def test_claude_hook_writes_presence_to_outbox():
    assert 'OUTBOX="$HOME/.claude/outbox"' in HOOK_SCRIPT
    assert 'emit_presence "$PAYLOAD"' in HOOK_SCRIPT


def test_claude_hook_script_supports_direct_hook_target_overrides():
    assert 'TARGET_URL="${LONGHOUSE_HOOK_URL:-}"' in HOOK_SCRIPT
    assert 'TARGET_TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"' in HOOK_SCRIPT
    assert "X-Agents-Token: $TARGET_TOKEN" in HOOK_SCRIPT
    assert "${TARGET_URL%/}/api/agents/presence" in HOOK_SCRIPT
    assert "LONGHOUSE_MANAGED_SESSION_ID" in HOOK_SCRIPT
    assert "LONGHOUSE_SESSION_ID" in HOOK_SCRIPT


def test_claude_stop_hook_forces_sidechain_for_hindsight_workspace():
    assert 'FORCE_SIDECHAIN="${LONGHOUSE_IS_SIDECHAIN:-0}"' in HOOK_SCRIPT
    assert 'HINDSIGHT_ROOT="$HOME/.claude/hindsight"' in HOOK_SCRIPT
    assert 'case "$CWD" in' in HOOK_SCRIPT


def test_session_start_hook_prefers_direct_hook_target_overrides():
    assert 'TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"' in SESSION_START_HOOK_SCRIPT
    assert 'URL="${LONGHOUSE_HOOK_URL:-}"' in SESSION_START_HOOK_SCRIPT
    assert 'if [[ -z "$TOKEN" ]] || [[ -z "$URL" ]]' in SESSION_START_HOOK_SCRIPT


def test_claude_stop_hook_entry_is_sync(tmp_path):
    """Stop hook is now sync (no shipping, just outbox write + binding seed)."""
    stop_entry, _lifecycle_entry, _session_start_entry = _make_hook_entries(tmp_path)
    hook = stop_entry["hooks"][0]
    assert hook["async"] is False
    assert hook["timeout"] == 5
