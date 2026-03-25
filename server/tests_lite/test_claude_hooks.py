"""Tests for Claude hook installation and Stop shipping behavior."""

from zerg.services.shipper.hooks import HOOK_SCRIPT
from zerg.services.shipper.hooks import SESSION_START_HOOK_SCRIPT
from zerg.services.shipper.hooks import _make_hook_entries


def test_claude_hook_script_detaches_stop_shipping_and_retries_until_file_exists():
    assert '[[ "$EVENT" == "Stop" ]] && [[ -n "$TRANSCRIPT" ]]' in HOOK_SCRIPT
    assert "nohup /bin/bash -c" in HOOK_SCRIPT
    assert "for delay in 0 0.25 0.5 1 2 4" in HOOK_SCRIPT
    assert 'if [[ "$delay" != "0" ]]; then' in HOOK_SCRIPT
    assert 'if [[ -f "$transcript" ]]' in HOOK_SCRIPT
    assert 'ship --file "$transcript" "${ship_args[@]}" --quiet >/dev/null 2>&1 || true' in HOOK_SCRIPT
    assert '[[ "$EVENT" == "Stop" ]] && [[ -n "$TRANSCRIPT" ]] && [[ -f "$TRANSCRIPT" ]]' not in HOOK_SCRIPT


def test_claude_hook_script_supports_direct_hook_target_overrides():
    assert 'TARGET_URL="${LONGHOUSE_HOOK_URL:-}"' in HOOK_SCRIPT
    assert 'TARGET_TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"' in HOOK_SCRIPT
    assert 'X-Agents-Token: $TARGET_TOKEN' in HOOK_SCRIPT
    assert '${TARGET_URL%/}/api/agents/presence' in HOOK_SCRIPT
    assert 'LONGHOUSE_SESSION_ID' in HOOK_SCRIPT
    assert '--url "$target_url"' in HOOK_SCRIPT
    assert '--token "$target_token"' in HOOK_SCRIPT
    assert '--session-id "$managed_session_id"' in HOOK_SCRIPT


def test_session_start_hook_prefers_direct_hook_target_overrides():
    assert 'TOKEN="${LONGHOUSE_HOOK_TOKEN:-}"' in SESSION_START_HOOK_SCRIPT
    assert 'URL="${LONGHOUSE_HOOK_URL:-}"' in SESSION_START_HOOK_SCRIPT
    assert "if [[ -z \"$TOKEN\" ]] || [[ -z \"$URL\" ]]" in SESSION_START_HOOK_SCRIPT


def test_claude_stop_hook_entry_is_async(tmp_path):
    stop_entry, _lifecycle_entry, _session_start_entry = _make_hook_entries(tmp_path)
    hook = stop_entry["hooks"][0]
    assert hook["async"] is True
    assert hook["timeout"] == 30
