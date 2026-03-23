"""Tests for Claude hook installation and Stop shipping behavior."""

from zerg.services.shipper.hooks import HOOK_SCRIPT
from zerg.services.shipper.hooks import _make_hook_entries


def test_claude_hook_script_backgrounds_stop_shipping_and_replays_once():
    assert "sleep 1" in HOOK_SCRIPT
    assert "ship --file \"$TRANSCRIPT\" --quiet" in HOOK_SCRIPT
    assert "&>/dev/null || true" in HOOK_SCRIPT
    assert ") &" in HOOK_SCRIPT


def test_claude_stop_hook_entry_is_async(tmp_path):
    stop_entry, _lifecycle_entry, _session_start_entry = _make_hook_entries(tmp_path)
    hook = stop_entry["hooks"][0]
    assert hook["async"] is True
    assert hook["timeout"] == 30
