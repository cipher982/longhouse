# No-Python Device Phase 3: Claude Native Control Slice

Status: Draft
Parent spec: `docs/specs/no-python-device-phase2.md`
Previous phase: `docs/specs/no-python-device-phase1.md`

## Goal

Phase 3 starts the Claude migration from Python-owned device behavior to the
native `longhouse-engine device claude ...` path.

The first shippable slice is Claude live control from the Machine Agent:

- `session.send_text`
- `session.interrupt`
- `session.steer_text`
- `session.answer_pause`

Today those commands are received by the Rust Machine Agent and then shell out
to the Python-packaged `longhouse claude-channel ...` command. This slice moves
that live-control hop into Rust while preserving Claude's existing channel
state format and HTTP injection contract.

## Product Boundary

This phase does not change the user-facing Claude launch UX yet.

`longhouse claude` and `longhouse claude-channel launch/serve` remain Python
compatibility paths until the later Claude launch slice ports the MCP stdio
server, user MCP config install, PTY wrapper, hook setup, and launch panel.

This slice is still valuable because it removes Python from the always-on
remote-control path after a Claude session is already managed and live.

## In Scope

- Add a native Rust Claude channel-control module in `longhouse-engine`.
- Read existing Claude channel state files from
  `~/.claude/channels/longhouse/sessions/<session-id>.json`.
- Send live text, steer text, and pause-response text through the existing
  local bridge HTTP `/inject` endpoint.
- Send `SIGINT` to the recorded Claude process for interrupts.
- Preserve the current result shape: provider `claude`, transport
  `claude_channel_bridge`, empty stdout/stderr, `exit_code: 0`.
- Keep tokens out of argv and logs. The channel auth token is read from the
  state file and sent as an HTTP header only.
- Update the Phase 1 inventory so `control-channel-claude-shellout` moves from
  `transitional_device` to `native_device` once the shellout is gone.
- Keep `claude-launch-wrapper`, `claude-channel-bridge`, and
  `claude-channel-helpers` transitional because launch/server behavior still
  lives in Python.

## Out of Scope

- Porting `longhouse claude` itself.
- Porting `longhouse claude-channel serve` or the MCP stdio bridge server.
- Porting detached Claude PTY launch.
- Changing Claude capability advertising beyond removing the Machine Agent's
  dependency on the Python CLI for live control.
- Changing visible launch flags, browser/iOS UX, or provider binary ownership.

## Design

### Native Control Module

Add an engine module, likely `engine/src/claude_channel_control.rs`, that owns
the Rust equivalent of these Python commands:

```text
longhouse claude-channel send --session-id <id> --text <text> [--meta key=value]
longhouse claude-channel interrupt --session-id <id>
```

The module should expose typed async functions:

- `send_text(session_id, text, meta)`
- `interrupt(session_id)`

`steer_text` and `answer_pause` should reuse `send_text` with explicit meta:

- `intent=steer`
- `intent=pause_response`
- `request_key=<...>`
- `decision=<...>`

### State Compatibility

Use the existing state file schema written by the Python MCP bridge:

```json
{
  "session_id": "...",
  "provider_session_id": "...",
  "auth_token": "...",
  "port": 12345,
  "claude_pid": 1234,
  "ready": true
}
```

Native control should fail clearly when:

- the state file is missing or invalid;
- `ready` is false;
- the state has no usable `port` or `auth_token` for send;
- the state has no usable `claude_pid` for interrupt;
- the local bridge rejects injection;
- the Claude process no longer exists.

Failure mapping should stay compatible with the existing control command
contract:

- missing, invalid, or not-ready state maps to a session-not-attached style
  error;
- bridge HTTP failures, timeouts, and rejected injections map to
  `command_failed`;
- invalid request payloads keep using `invalid_command`;
- the channel auth token must not appear in any error message.

The Rust path should preserve the Python send timing shape: wait up to roughly
10 seconds for a ready state file, polling at short intervals, then use a short
HTTP timeout for the local injection request. The bridge injection itself is a
single request with no retry, matching the Python compatibility command.

### State Root

Default state root stays:

```text
~/.claude/channels/longhouse
```

Tests may use an explicit state root. The production control path should use the
default unless a future debug flag is intentionally introduced.

### No Python Shellout

The Rust Machine Agent must stop calling `run_longhouse_command` for Claude
send, interrupt, steer, and answer-pause.

`run_claude_channel_command` should be deleted or made impossible to call. The
Phase 1 inventory should then mark `control-channel-claude-shellout` as
`native_device`, and tests should fail if a Claude live-control route points
back through `longhouse`, `python`, `uv`, or `pip`.

The proof should be positive, not just absence-based. The inventory/engine
tests must verify that Claude `send`, `interrupt`, `steer`, and
`answer_pause` dispatch through a Rust-native Claude module from
`engine/src/control_channel.rs`.

### Interrupt Semantics

The Python compatibility command currently sends `SIGINT` to the recorded
Claude process PID. The native implementation should prefer interrupting the
Claude process group on Unix when available, falling back to the recorded PID
only when process-group signaling fails or is unavailable. This better matches
terminal behavior while keeping the existing state schema.

This is safe because the current Python detached launcher starts Claude in a
new session/process group. The later native launch slice must preserve that
process-group isolation before relying on process-group interrupts.

## Success Criteria

- Claude live control from the Machine Agent no longer requires the Python
  `longhouse` CLI.
- `control-channel-claude-shellout` is marked `native_device` in the Phase 1
  inventory.
- `make validate-no-python-device-path` reports that specific Claude shellout
  as native while other Claude launch/server Python debt remains transitional.
- Engine tests cover:
  - send injects text with `injected_by=longhouse` and
    `longhouse_session_id`;
  - steer injects `intent=steer`;
  - answer-pause injects pause metadata and returns the existing
    `pause_response` result payload;
  - interrupt sends `SIGINT` to the Claude process group or recorded PID;
  - missing state / bad bridge responses fail without leaking auth tokens.
- The no-Python inventory has a positive native-dispatch assertion for the
  Claude live-control route, not only a deleted shellout symbol.
- `make validate-native-device-entrypoints` still passes and refuses to mark
  the whole Claude command group native while launch/server Python debt remains.
- No user-visible Claude launch behavior changes in this slice.

## Suggested Checks

- `make test-engine`
- `make validate-no-python-device-path`
- `make validate-native-device-entrypoints`
- `make validate-managed-session-contract`

Run broader `make validate` before shipping if the focused checks are clean.

## Later Claude Slices

This slice leaves the following Phase 3 work for later commits:

1. Native `longhouse-engine device claude` command namespace and compatibility
   shim behavior.
2. Native Claude launch prereq setup: hook install, user MCP config install,
   auth/channel detection, and launch payload creation.
3. Native detached PTY launch and channel readiness wait.
4. Native MCP stdio channel server, or an explicit product decision that this
   one Python helper remains packaged separately until a larger server-runtime
   split.

## Proof-Surface Correction

After the native channel-control/server slices, the no-Python inventory must
still treat Claude remote-approve as incomplete while
`~/.claude/hooks/longhouse-permission-gate.py` is a Python hook installed on
the device. That file is always installed by the hook repair path; it executes
Python only when remote-approve mode enables the permission hook. The lifecycle
hook (`longhouse-hook.sh`) is `native_exempt` because its installed script text
does not require Python, but the permission gate is `transitional_device`
`hook_script` debt until it is replaced natively or explicitly excluded from
the no-Python device promise.
