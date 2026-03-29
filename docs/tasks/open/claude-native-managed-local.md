# Claude Native Managed-Local

Status: In progress
Spec: `docs/specs/claude-native-managed-local.md`
Last updated: 2026-03-29

## Goal

Replace the current `longhouse claude` tmux path with a native Claude launch on `this-device` while preserving live server-driven injection via a local channel bridge. Keep generic runner-launched Claude sessions on tmux for now so the refactor does not break remote launch flows.

## Done when

- `longhouse claude` launches native Claude on this device with `managed_transport=claude_channel_bridge`.
- Longhouse can inject a live message into the active Claude session through the new bridge transport.
- Generic Claude `/managed-local` launch still uses tmux and keeps existing behavior.
- Automated tests cover transport resolution, API launch semantics, CLI launch semantics, and bridge notification delivery.

## Checklist

- [ ] Add the spec and transport split (`claude_channel_bridge`)
- [ ] Refactor managed-local launch resolution so Claude `this-device` no longer goes through tmux
- [ ] Add the local Claude channel server + send/interrupt helpers
- [ ] Update `longhouse claude` to ensure config and launch native Claude with channel flags
- [ ] Add unit/API/CLI tests for the new transport
- [ ] Add a subprocess E2E test for MCP init + `notifications/claude/channel` emission
- [ ] Run targeted tests and verify the native Claude path locally without deploying hosted instances

## Notes

- Bedrock patching is intentionally not the default architecture in this task; leave room for it as a later compatibility layer.
- Claude channel registration should use Claude's local-scope MCP config in `~/.claude.json`, keyed by canonical workspace path. Do not write `.mcp.json` into the repo.
- Avoid hosted/user-instance deploys while the current backfill work is in progress.
