"""Provider CLI ownership constants shared by CLI and local health surfaces."""

from zerg.managed_provider_contract_manifest import managed_provider_contract_items

CLAUDE_BIN = "claude"
CODEX_BIN = "codex"
OPENCODE_BIN = "opencode"
ANTIGRAVITY_BIN = "agy"
CODEX_BIN_ENV = "LONGHOUSE_CODEX_BIN"
OPENCODE_BIN_ENV = "LONGHOUSE_OPENCODE_BIN"
ANTIGRAVITY_BIN_ENV = "LONGHOUSE_ANTIGRAVITY_BIN"
PROVIDER_CLI_BINARY_BY_PROVIDER = {str(item["provider"]): str(item["provider_cli_binary"]) for item in managed_provider_contract_items()}
PROVIDER_CLI_ENV_BY_PROVIDER = {
    str(item["provider"]): (str(item["provider_cli_env"]) if item.get("provider_cli_env") else None)
    for item in managed_provider_contract_items()
}
LEGACY_MANAGED_CODEX_LAUNCHER_MARKER = "# longhouse-managed-codex-launcher"
PROVIDER_CLI_SOURCE_BRIDGE_STATE = "bridge_state"
PROVIDER_CLI_SOURCE_ANTIGRAVITY_BIN_FLAG = "--agy-bin"
PROVIDER_CLI_SOURCE_CODEX_BIN_FLAG = "--codex-bin"
PROVIDER_CLI_SOURCE_OPENCODE_BIN_FLAG = "--opencode-bin"
PROVIDER_CLI_SOURCE_MISSING = "missing"
PROVIDER_CLI_SOURCE_PATH = "PATH"
PROVIDER_CLI_SOURCE_PROCESS = "process"
