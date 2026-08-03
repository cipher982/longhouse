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
    "cursor_observed_install": ("cursor_observed_install_contract_preserved",),
    **{
        f"{provider}_conversation_reset": (
            "provider_conversation_reset_observed",
            "raw_history_conserved_across_reset",
            "longhouse_reset_binding_current",
        )
        for provider in ("codex", "claude", "opencode", "antigravity", "cursor")
    },
}


def assertions_for(scenario_id: str) -> tuple[str, ...]:
    return ASSERTIONS_BY_SCENARIO[scenario_id]
