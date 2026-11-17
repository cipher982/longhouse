# @generated from schemas/managed_providers.yml by scripts/generate/managed_identity_contract_rs.py -- do not edit by hand.
# Run: make generate-managed-identity
"""Each provider's execution owner for exact process-exit run evidence.

The engine stamps (process_role, source) from the same schema row, so the
Runtime Host and the engine cannot name different owners.
"""

RUN_EXIT_AUTHORITY: dict[str, tuple[str, str]] = {
    "antigravity": ("provider", "antigravity_hook_state"),
    "claude": ("provider", "claude_channel_scan"),
    "codex": ("app_server", "codex_bridge_scan"),
    "cursor": ("launcher", "cursor_helm_scan"),
    "omp": ("provider", "omp_helm_scan"),
    "opencode": ("provider", "opencode_server_scan"),
    "pi": ("provider", "pi_helm_scan"),
}
