# Machine Agent Control Channel

Status: Proposed launch simplification; reviewed by Hatch Opus
Owner: local runtime + managed session control
Updated: 2026-05-06

## Goal

Move managed-session remote control from the standalone Runner into the Rust
Machine Agent (`longhouse-engine`), starting with Codex.

Longhouse's core value prop is durable session sync plus a real control path for
sessions launched through Longhouse. That control path should belong to the same
local service that already owns transcript shipping, local runtime state, and
provider bridges.

Target product model:

```text
Browser / iOS / API
        |
        v
Runtime Host session-control route
        |
        v
Machine Agent control WebSocket
        |
        v
Provider-specific control path
        |
        v
Codex / Claude / future provider session
```

## Non-Goals

- Do not embed arbitrary `exec.full` remote shell behavior into the Machine
  Agent.
- Do not replace SSH for general user infrastructure work.
- Do not vendor or distribute provider CLIs.
- Do not revive cloud takeover or implicit local-to-cloud migration.
- Do not make Autopilot active just because a control channel exists.
- Do not solve the future of generic Runner exec in this spec.

Generic remote exec may survive as an optional capability later, but it must not
be the foundation for managed-session control.

## Current Shape

Runner currently does more than optional remote exec:

- Managed `this-device` launch used to resolve the calling device as a Runner
  and require that Runner to be online.
- Live send, interrupt, and Codex steer still build provider-specific shell
  commands, then deliver those commands through `RunnerJobDispatcher`.
- Session capability and host-state logic still key off `source_runner_id` and
  Runner WebSocket online state.

This leaves the product with the wrong dependency graph:

```text
managed session control -> generic remote shell Runner -> provider bridge
```

The desired dependency graph is:

```text
managed session control -> Machine Agent -> provider bridge
```

## First-Principles Invariants

1. **One local execution owner.**
   The Machine Agent is the local owner for Longhouse-managed session control.

2. **Typed control, not remote shell.**
   Runtime Host sends explicit session-control commands. It does not send
   arbitrary shell strings to the Machine Agent.

3. **Best-effort transport with explicit retry.**
   The current Runner path is synchronous request/response with a short timeout.
   The engine path should preserve that behavior. If Runtime Host or the engine
   disconnects mid-send, the user/client retries.

4. **Request idempotency, not a command database.**
   Each command frame has a `command_id`. The engine keeps a short-lived LRU of
   completed command ids to prevent duplicate injection during retries or double
   clicks. Do not add a durable command queue for launch.

5. **Provider binaries remain user-owned.**
   The Machine Agent may talk to provider CLIs or provider app servers, but it
   does not install, vendor, patch, or pin them.

6. **No hidden fallback.**
   If a session is controlled by legacy Runner dispatch during migration, the
   capability and health surfaces should say so. The system must not silently
   switch transports.

## Command Envelope

The launch command model is a WebSocket request/response envelope, not a table.

Initial command types:

- `session.send_text`
- `session.interrupt`
- `session.steer_text` (Codex and Claude only today)

Runtime Host sends:

```json
{
  "type": "command",
  "command_id": "8c620d1d-8c59-4c7e-a247-832732d0ad9e",
  "session_id": "c3026405-5e99-447f-ae5c-baacd848ac47",
  "command_type": "session.send_text",
  "payload": {
    "text": "continue"
  }
}
```

Machine Agent replies:

```json
{
  "type": "command_result",
  "command_id": "8c620d1d-8c59-4c7e-a247-832732d0ad9e",
  "ok": true,
  "result": {
    "provider": "codex",
    "transport": "codex_app_server",
    "verified_turn_started": true
  }
}
```

Failure replies use the same envelope:

```json
{
  "type": "command_result",
  "command_id": "8c620d1d-8c59-4c7e-a247-832732d0ad9e",
  "ok": false,
  "error": {
    "code": "session_not_attached",
    "message": "Managed Codex session is not attached"
  }
}
```

No `queued`, `leased`, `expired`, or durable status FSM exists in the launch
path. If the channel is unavailable, Runtime Host returns an explicit
control-offline error and the caller can retry.

## Machine Control Channel

Add an authenticated Machine Agent WebSocket under the canonical machine
surface:

```text
GET /api/agents/control/ws
X-Agents-Token: <device token>
```

The Machine Agent sends a hello frame:

```json
{
  "type": "hello",
  "schema_version": 1,
  "device_id": "cinder",
  "machine_name": "cinder",
  "engine_build": "29db1495",
  "supports": [
    "codex.send",
    "codex.interrupt",
    "codex.steer",
    "claude.send",
    "opencode.send",
    "antigravity.send"
  ]
}
```

Runtime Host keeps an in-memory registry:

```text
device_id -> websocket + last_seen_at + supports[]
```

This mirrors the current Runner online registry but is scoped to typed
managed-session control. It is deliberately not a generic job queue.

The channel is an outbound connection from the user's machine to the Runtime
Host, so it works through NAT and does not require inbound access to laptops.

## Engine Responsibilities

`longhouse-engine connect` should own this loop beside shipping:

- authenticate with the existing device token
- announce supported provider-control operations
- maintain the control WebSocket with bounded reconnect/backoff
- receive typed commands for its own `device_id`
- validate that the target session is locally managed and known
- dispatch typed commands to the local provider control path
- return one command result frame
- update engine status/local-health with control-channel state

The Machine Agent rejects commands when:

- the session is not known locally
- the session is unmanaged
- the requested command is unsupported by the provider transport
- the provider control path is missing, detached, or stale
- the command id was already completed recently

## Provider Dispatch

Provider-specific mechanics stay split.

### Codex

Codex is the first target because `longhouse-engine` already owns the Codex
bridge and relay.

Target behavior:

- `session.send_text` routes through the Codex app-server bridge/relay.
- `session.interrupt` routes through the same bridge.
- `session.steer_text` remains Codex-only.
- Verification uses the same hook/runtime observations currently used by
  `managed_local_control.py`.

Avoid shelling out to `longhouse-engine codex-bridge ...` from the running
engine. Pull the reusable bridge operations behind internal Rust functions.

### Claude

Claude control goes through the local Claude channel helper exposed by the
Python CLI. The engine advertises `claude.launch/send/interrupt/steer` only
when the local `claude` binary and Longhouse CLI are available, and dispatches
through the typed `claude-channel` adapter. Do not recreate a detached Claude
bridge daemon.

### OpenCode

OpenCode control goes through the local OpenCode server bridge exposed by the
Python CLI. The engine advertises `opencode.launch/send/interrupt` only when
the stock `opencode` binary and Longhouse CLI are available, starts
`opencode serve` through `opencode-channel launch`, and drives the provider via
OpenCode's localhost server API. Do not advertise `opencode.steer` until
active-turn injection is proven.

### Antigravity

Antigravity control goes through the local hook inbox exposed by the Python
CLI. The engine advertises `antigravity.send` only when the stock `agy` binary
and Longhouse CLI are available, and dispatches `session.send_text` through
`antigravity-channel send`. This is queued input claimed by active
`PreInvocation`/`PostInvocation` hooks, not active-turn steer, reattach, remote
launch, or interrupt.

### Future Providers

Do not infer future provider behavior from Codex or Claude. Add one typed
provider adapter at a time when a real control primitive exists.

## Runtime Host Responsibilities

The Runtime Host keeps the browser/iOS/API surface stable.

Existing endpoints such as:

- `POST /api/sessions/{id}/send-live`
- `POST /api/sessions/{id}/interrupt`
- `POST /api/sessions/{id}/steer`

should call one managed-control dispatch service that:

- checks current session capability
- chooses an explicit transport:
  - `engine_channel` for supported Codex sessions on machines with online engine
    control
  - `legacy_runner` for sessions that still require Runner
- sends one command frame or Runner job
- waits up to the existing short timeout
- returns the same success/error shape callers already expect

No frontend concept changes in the first migration.

## Capability And Health

The key launch change is narrow:

- for Codex managed sessions, replace the `source_runner_id is not None` live
  control gate with `engine_channel_online(session.device_id)`
- for Claude and historical sessions, keep Runner-backed capability explicit as
  `legacy_runner` until a native engine path exists

Local health already separates transcript ingest from managed-control
degradation. During migration:

- Runner drift degrades only legacy Runner-backed managed control.
- Missing Runner must not degrade transcript ingest or local launch.
- Once Codex uses `engine_channel`, Runner drift must no longer affect Codex
  live-control capability.

## Migration Plan

### Phase 0: Stop New Runner Coupling

Already started in `machine-agent-control-refactor`:

- `this-device` managed launch no longer requires Runner readiness.
- Runner config drift no longer blocks local launch or install.
- Local health still degrades managed-control state when Runner-backed control
  is the active transport.

### Phase 1: Backend Dispatch Seam

Add a small managed-control dispatch service.

Current code has three managed-control dispatch points:

- send text
- interrupt
- Codex steer

The seam should do only this:

```text
session_chat route
        |
        v
managed_control_dispatcher
        |
        +-- engine_channel transport
        +-- legacy_runner transport
```

Default all sessions to `legacy_runner` at first. Tests should prove current
behavior is unchanged and transport selection is explicit.

Acceptance:

- Existing Runner-backed send/interrupt/steer tests pass.
- New sessions without Runner metadata are not advertised as remotely live
  until engine control exists.
- The dispatcher does not introduce a queue, status table, or command FSM.

### Phase 2: Codex Control Over Engine Channel

Land the engine WebSocket and Codex control dispatch together. Do not merge a
WebSocket skeleton that cannot execute a command.

Runtime Host:

- add `/api/agents/control/ws`
- add in-memory `ControlChannelRegistry`
- add request/response tracking by `command_id`
- route Codex `send_text`, `interrupt`, and `steer_text` through
  `engine_channel` when available

Engine:

- connect to `/api/agents/control/ws`
- send hello/supports frame
- execute Codex send/interrupt/steer through internal bridge functions
- keep a short-lived completed-command LRU
- return command results

Acceptance:

- A managed Codex session with no Runner can receive send text from web/iOS.
- Codex interrupt works without Runner.
- Codex steer works without Runner for active turns.
- Existing verification semantics remain intact.
- Runner offline does not affect Codex live-control capability.

### Phase 3: Codex Capability And Identity Cleanup

Once Phase 2 works:

- stop writing `source_runner_id` for new Codex managed sessions
- remove `source_runner_id` from Codex `supports_live_control`
- replace `managed_runner_host_state` for Codex with engine-channel state
- update local-health copy so Codex control reports `engine_channel`, not
  Runner

Acceptance:

- New Codex managed sessions do not need a Runner row.
- Historical Runner-backed Codex sessions still render honestly.
- UI copy says managed/control-offline/reattachable based on Machine Agent
  facts for new Codex sessions.

### Phase 4: Claude Decision

Claude is a separate decision after Codex proves the channel.

Options:

- keep Claude on explicit `legacy_runner` transport for launch
- port the minimal Claude channel client into Rust
- accept a narrow typed Python helper adapter inside the engine

Do not make this choice in the Codex migration.

## Testing Plan

Backend:

- dispatcher selects legacy Runner by default
- dispatcher selects engine channel for supported Codex sessions when online
- dispatcher returns explicit control-offline when engine channel is offline and
  no legacy transport applies
- Codex live capability uses engine-channel state, not Runner state
- legacy Runner transport remains explicit during migration

Engine:

- control WebSocket connects, reconnects, and authenticates
- unknown session rejection
- unmanaged session rejection
- duplicate command id rejection from LRU
- Codex send/interrupt/steer adapter tests

End-to-end:

- managed Codex session launched with no Runner can be controlled from web/iOS
- Runner offline no longer affects Codex managed control once engine transport
  is active
- transcript ingest and managed control failures show as separate health lanes

## Deletion Targets

After Codex control moves to engine:

- remove Codex managed-control calls into `RunnerJobDispatcher`
- remove `source_runner_id` capability gating for Codex
- stop writing `source_runner_id` on new Codex managed launches
- remove Runner config checks from Codex managed-control health

Do not delete the Runner package in this spec. Generic remote exec is a
separate product decision.

## Deferred Questions

- When should Claude move off `legacy_runner`?
- Does generic remote exec have enough product usage to survive as optional
  Runner/engine exec?
- After Codex and Claude both leave Runner, should historical `source_runner_*`
  fields be migrated or left as read-only legacy metadata?
