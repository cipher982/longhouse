# Native OpenCode Control

Status: Phase 1 approved

## Executive Summary

Longhouse currently treats OpenCode managed sessions as first-class from the Runtime Host, but the Machine Agent still shells out to the Python `longhouse opencode-channel` command for OpenCode send and interrupt. The migration target is engine-native OpenCode control while preserving the existing user-owned provider model: stock `opencode serve` remains the execution owner, Longhouse owns only the bridge state and control path.

The first shippable phase moves only OpenCode send and interrupt into Rust. Remote launch, attach, stop, and lifecycle cleanup stay on the existing Python-backed path until later phases.

## Decision Log

### Decision: Start With Send And Interrupt

**Context:** OpenCode send/interrupt already have stable HTTP semantics through the server bridge: `prompt_async` for input and `abort` for interrupt.
**Choice:** Port those two operations first and leave launch/stop unchanged.
**Rationale:** This removes the hot-path Python shellout while keeping lifecycle ownership and idempotent launch behavior stable.
**Revisit if:** OpenCode changes the local server API or introduces a native mid-turn steering semantic.

### Decision: Keep Launch Python-Backed In Phase 1

**Context:** Launch owns process setup, private runtime config, idempotency locks, token secrecy, and terminal-owned lifecycle modes.
**Choice:** Do not change OpenCode launch in Phase 1.
**Rationale:** Send/interrupt are bounded request adapters; launch has broader lifecycle blast radius and should be ported as its own phase.
**Revisit if:** send/interrupt require state fields that only a native launch writer can safely provide.

## Architecture

Phase 1 adds a Rust OpenCode control adapter in the Machine Agent:

- read `~/.claude/managed-local/opencode-server/{session_id}.json`
- tolerate the existing readable state schema range
- require matching Longhouse session id, provider session id, local server URL, and password
- default the username to `opencode`
- reject non-local or non-HTTP server URLs before sending credentials
- POST send input to `/session/{provider_session_id}/prompt_async?directory={cwd}`
- POST interrupt to `/session/{provider_session_id}/abort?directory={cwd}`
- return the same control result shape the Runtime Host already expects

The Runtime Host and provider contract manifest stay capability-driven. OpenCode active-turn steer remains unsupported.

## Implementation Phases

### Phase 1: Native OpenCode Send/Interrupt

Status: Implemented and approved

Review: Hatch DeepSeek approved commit `7dca166f3` against the Phase 1 acceptance criteria.

Coverage hardening: Added focused regressions for control-channel routing, empty `cwd`, default username, provider-session path encoding, and invalid/incompatible state files.

Goal: Remove the Python CLI shellout for OpenCode `session.send_text` and `session.interrupt`.

Steps:

1. Add focused Rust tests for bridge-state reading and request construction.
2. Add the Rust OpenCode control adapter.
3. Route OpenCode send/interrupt in `control_channel` through the adapter.
4. Remove or update stale `opencode-channel` argument tests for send/interrupt.
5. Run focused engine tests.

Acceptance criteria:

- OpenCode send posts `{"noReply": true, "parts": [{"type": "text", "text": ...}]}` to `/session/{provider_session_id}/prompt_async`.
- OpenCode interrupt posts to `/session/{provider_session_id}/abort`.
- Both requests use Basic auth from the private bridge state file and include the stored `cwd` as the `directory` query parameter when present.
- Non-local OpenCode server URLs are rejected before credentials are sent.
- `control_channel.rs` no longer shells out to `longhouse opencode-channel send` or `longhouse opencode-channel interrupt`.
- OpenCode launch still uses the existing launch path.

Test commands:

```bash
make test-engine
```

### Phase 2: Contract And Gate Cleanup

Goal: Make capability advertisement reflect native operation ownership without pretending launch is native.

Acceptance criteria:

- Provider support bits still require stock `opencode` on PATH.
- Any provider-wide `requires_longhouse_cli` semantics are split or kept conservative until launch is native.
- Runtime Host tests prove browser/iOS sends route through Machine Agent supports for `opencode.send` and `opencode.interrupt`.

### Phase 3: Native OpenCode Launch

Goal: Port idempotent OpenCode server-bridge launch into Rust.

Acceptance criteria:

- Launch preserves token secrecy, private state permissions, idempotent session locks, and lifecycle modes.
- Existing `attached_tui`, `keep_server`, and `detached` behavior remains compatible with older state files.
- Launch has no hidden fallback to unmanaged import.

### Phase 4: Native Stop/Terminate

Goal: Port OpenCode stop/terminate behavior into Rust.

Acceptance criteria:

- Stop uses recorded process identity before signaling.
- PID reuse cannot kill an unrelated process.
- Attached-TUI reaper behavior stays limited to the documented launch mode.

### Phase 5: Full Edge Migration

Goal: Retire Python ownership of managed OpenCode control once send, interrupt, launch, attach, stop, and proof paths are native.

Acceptance criteria:

- Python remains only as CLI packaging/user entrypoint where appropriate.
- Machine Agent owns provider runtime control adapters.
- Docs, SLA config, and release proof reflect the final ownership split.
