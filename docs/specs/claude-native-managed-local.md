# Claude Native Managed-Local

Status: Active
Last updated: 2026-03-29

## Goal

Make `longhouse claude` behave like a native Claude session on the local machine while preserving Longhouse's ability to inject live user messages into the active session. The first supported path is Claude Code Channels on `this-device`; tmux remains the fallback for generic runner-launched Claude sessions so we do not regress remote launch flows during the refactor.

## Product boundary

- Primary OSS path: Claude subscription / local Claude.ai login with native Claude TUI and Longhouse channel bridge.
- Existing generic `/managed-local` runner launch remains tmux-backed for Claude in this phase.
- Codex native bridge stays unchanged.
- Bedrock patching is explicitly out of scope for this first implementation slice. The architecture should leave room for it without making it the default story.

## Architecture

### Transport split

Add a third managed-local transport:

- `tmux`
- `codex_app_server`
- `claude_channel_bridge`

Resolution rules for this phase:

- `provider=codex` -> `codex_app_server`
- `provider=claude` and launch target is `this-device` -> `claude_channel_bridge`
- all other Claude launches -> `tmux`

### Launch shape

For `claude_channel_bridge`:

1. The API creates the `AgentSession` row and returns immediately.
2. `longhouse claude` ensures the local Claude channel server is registered in Claude's local-scope MCP config inside `~/.claude.json`, keyed by the canonical workspace path.
3. `longhouse claude` launches Claude directly in the foreground with:
   - the provider session id
   - the Longhouse session id exported via env
   - the Longhouse channel dev-server flag
4. Claude spawns the Longhouse channel server as an MCP stdio subprocess.
5. The channel server opens a localhost ingress and writes a state file for the session.

### Control shape

- Browser/Loop send -> Longhouse server -> runner dispatch -> `longhouse claude-channel send --session-id ... --text ...`
- The send subcommand reads the local state file and POSTs to the bridge ingress.
- The channel server emits `notifications/claude/channel` into the active Claude session.
- Interrupts use `longhouse claude-channel interrupt --session-id ...`, targeting the Claude process associated with the bridge state.

### Config shape

- Keep hook installation in `~/.claude/settings.json`, which Longhouse already owns.
- Register the channel bridge as a Claude local-scope MCP server in `~/.claude.json` under the canonical workspace path.
- Do not write `.mcp.json` into the repo and do not add a user-global MCP server for this transport.

## Success criteria

### Product

- `longhouse claude` launches a native Claude TUI on the local machine instead of printing a tmux attach command.
- Longhouse can inject a live message into that active Claude session through the new bridge transport.
- Existing generic Claude managed-local launch (`/api/sessions/managed-local`) still works with tmux.

### API and transport

- `/api/sessions/managed-local/this-device` returns `managed_transport="claude_channel_bridge"` for Claude.
- Generic `/api/sessions/managed-local` still returns `managed_transport="tmux"` for Claude.
- Session detail / attach command generation works for the new Claude transport.
- Managed-local send routing uses bridge commands for `claude_channel_bridge`, tmux for `tmux`, and engine RPC for `codex_app_server`.

### Verification

- Unit tests cover transport resolution and command builders.
- API tests cover native Claude `this-device` launch and generic Claude tmux fallback.
- CLI tests cover native Claude launch behavior and local-scope MCP registration.
- A subprocess E2E test initializes the actual Longhouse Claude channel server over MCP stdio, triggers `send`, and verifies a `notifications/claude/channel` frame is emitted.

## Non-goals for this slice

- Shipping the Bedrock binary patcher or compatibility launcher
- Replacing generic runner-launched Claude sessions with native attach
- Supporting API-key-only Claude users with a new fallback transport
- Deploying hosted/user instances while the current backfill work is active
