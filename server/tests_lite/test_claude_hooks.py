"""Tests for Claude hook installation and Stop shipping behavior."""

from zerg.services.shipper.hooks import HOOK_SCRIPT
from zerg.services.shipper.hooks import _make_hook_entries


def test_claude_hook_script_detaches_stop_shipping_and_retries_until_file_exists():
    assert '[[ "$EVENT" == "Stop" ]] && [[ -n "$TRANSCRIPT" ]]' in HOOK_SCRIPT
    assert "nohup /bin/bash -c" in HOOK_SCRIPT
    assert "for delay in 0 1 2 4" in HOOK_SCRIPT
    assert 'if [[ -f "$transcript" ]]' in HOOK_SCRIPT
    assert 'ship --file "$transcript" --quiet >/dev/null 2>&1 || true' in HOOK_SCRIPT
    assert '[[ "$EVENT" == "Stop" ]] && [[ -n "$TRANSCRIPT" ]] && [[ -f "$TRANSCRIPT" ]]' not in HOOK_SCRIPT


def test_claude_stop_hook_entry_is_async(tmp_path):
    stop_entry, _lifecycle_entry, _session_start_entry = _make_hook_entries(tmp_path)
    hook = stop_entry["hooks"][0]
    assert hook["async"] is True
    assert hook["timeout"] == 30
