"""Declared semantic postconditions for public provider release qualification."""

from __future__ import annotations

ASSERTIONS_BY_SCENARIO = {
    "claude_real_print": (
        "claude_cli_channel_contract_preserved",
        "real_print_marker_returned",
    ),
    "opencode_server_contract": (
        "serve_session_contract_preserved",
        "process_restart_reattach_preserved",
    ),
    "antigravity_hook_inbox": (
        "hook_inbox_contract_preserved",
        "real_print_injection_observed",
    ),
}


def assertions_for(scenario_id: str) -> tuple[str, ...]:
    return ASSERTIONS_BY_SCENARIO[scenario_id]
