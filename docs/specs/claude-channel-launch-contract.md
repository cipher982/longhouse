# Claude Channel Launch Contract

Longhouse's managed Claude support is a control-path contract, not a best-effort
transcript import. If the Claude channel cannot load and report ready, the
managed launch must fail visibly instead of silently degrading to an unmanaged
session.

## Contract

- Longhouse launches the user's stock `claude` binary.
- Longhouse registers `longhouse-channel` as a user-scope MCP server in
  Claude's effective config.
- Longhouse starts Claude with:
  `--dangerously-load-development-channels server:longhouse-channel`.
- Longhouse keeps `LONGHOUSE_MANAGED_SESSION_ID`,
  `LONGHOUSE_CHANNEL_SESSION_ID`, and `LONGHOUSE_PROVIDER_SESSION_ID` in the
  provider process environment.
- Runtime Host records the launch as live only after channel state appears and
  becomes ready.

The development-channel flag is intentional. Longhouse's channel is a private
local MCP server and is not an Anthropic allowlisted channel plugin. The flag
should remain centralized in `build_claude_channel_exec_command()` so CLI
launch, attach, and Machine Agent remote launch cannot drift apart.

## Drift Detection

`longhouse provider-live canary --provider claude` must probe the installed
Claude binary for this exact development-channel launch shape without spending
provider tokens. The repo script `scripts/qa/provider-live-canary.py` is a
wrapper around that packaged canary. The explicit release-canary lane reports
channel launch and channel prompt delivery separately from provider execution.
A run where the Longhouse channel accepts input but Claude never writes the
expected assistant marker is a provider-execution failure, with terminal
auth/API prompts recorded only as diagnostic hints.
