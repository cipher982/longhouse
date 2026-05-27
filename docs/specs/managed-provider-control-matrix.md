# Managed Provider Control Matrix

Status: Draft
Owner: Machine Agent + managed provider CLI surfaces
Updated: 2026-05-27
Related: `provider-cli-contracts-and-codex-release-canaries.md`, `remote-session-launch.md`, `machine-agent-control-channel.md`, `managed-session-stall-recovery.md`

## Purpose

Longhouse should support every launch-scope agent app through explicit,
provider-specific contracts. The shared product surface is one matrix:

- `launch_local`
- `launch_remote`
- `reattach`
- `send_input`
- `interrupt`
- `steer_active_turn`
- `terminate`
- `tail_output`
- `runtime_phase`
- `transcript_binding`
- `release_canary`

The implementation rule is the same as the Codex incident lesson: do not hide
provider mechanics behind a fake generic abstraction. Provider adapters may
share helper code, but each operation must declare the provider transport it
actually uses and the failure mode it can prove.

## Capability Meanings

`send_input` means Longhouse can deliver a user message to the provider session.
It may be idle-turn input, async prompt input, or a provider-native channel
message, depending on the provider.

`steer_active_turn` means Longhouse can deliver corrective text while the
provider is in a fresh active phase. The session input API must reject explicit
`intent=steer` when runtime phase is not fresh `thinking` or `running`. If a
provider cannot prove that distinction, it may not advertise
`steer_active_turn`.

`interrupt` means Longhouse can ask the provider to stop the active turn. It is
not the same as process termination.

`launch_remote` means browser/iOS can ask the target Machine Agent to start a
new managed provider session without opening a local terminal. It must not
depend on the legacy Runner shell path.

## Current Matrix

| Provider | Local Launch | Remote Launch | Send | Interrupt | Steer | Runtime | Transcript | Current Truth |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex | Yes, `longhouse codex` | Yes, engine `session.launch` | Yes, engine channel | Yes, engine channel | Yes, engine channel with active-turn errors | Bridge/runtime events | Hooks + rollout | First-class |
| Claude | Yes, `longhouse claude` | Not first-class; local/Runner-era only | Yes, `claude-channel send` | Yes, `claude-channel interrupt` | Yes, gated by fresh active runtime phase, delivered through channel metadata | Channel/hooks/process scan | Claude channel + transcript ingest | First-class local, remote-launch gap |
| OpenCode | Yes, `longhouse opencode` | No | No | No | No | OpenCode plugin runtime events | Plugin/transcript observation | Observe-only managed wrapper |
| Antigravity | Yes, `longhouse antigravity` / `longhouse agy` | No | No | No | No | JSON hooks + runtime outbox | Hook binding to transcript path | Observe-only managed wrapper |

## Provider Contracts

### Codex

Codex is the reference implementation: an engine-owned bridge starts stock
`codex app-server`, creates or resumes the thread, relays WebSocket protocol,
and exposes `send`, `interrupt`, `steer`, `attach`, detached-UI launch, and
release canaries.

Keep Codex as the high bar, not as a generic shape forced onto other providers.

### Claude

Claude uses the native channel bridge:

- local launch: `longhouse claude`
- send: `longhouse claude-channel send --session-id <id> --text <text>`
- steer: same command with `--meta intent=steer`
- interrupt: `longhouse claude-channel interrupt --session-id <id>`

Steer is now a first-class Longhouse operation, but with an explicit active
runtime gate before dispatch. Longhouse does not claim that idle channel
injection is steer. If runtime phase is stale or idle, `intent=steer` returns
`turn_not_active`.

Next Claude gaps:

1. Move browser/iOS remote launch off the legacy Runner path and onto the
   Machine Agent control channel.
2. Add a `claude.launch` advertised support bit.
3. Add a detached local launch shape, even if it is just a terminal-owned
   provider process with channel state and no visible TUI.
4. Add a canary that proves channel `send`, active-turn `steer`, idle steer
   rejection, and `interrupt`.

### OpenCode

OpenCode is the closest next provider to full control because it already has a
server model. The current CLI exposes `opencode serve`, `opencode attach`, and
`opencode run --attach <server>`. The local server's `/doc` OpenAPI payload
exposes session create/list, prompt, async prompt, wait, abort, and TUI prompt
append/submit endpoints.

Target OpenCode adapter:

1. Launch an engine-owned OpenCode server sidecar:
   - stock `opencode serve --hostname 127.0.0.1 --port 0`
   - `OPENCODE_CONFIG_CONTENT` includes the Longhouse runtime plugin
   - state file records `session_id`, `server_url`, pid/pgid, auth, cwd, and
     provider session id
2. Create or resolve the OpenCode session through the server API.
3. Implement `longhouse opencode-channel send` against
   `/api/session/:id/prompt`, `/api/session/:id/prompt_async`, or the stable
   legacy `/session/:id/message`/`prompt_async` endpoint after a canary proves
   the exact versioned shape.
4. Implement interrupt against `/session/:id/abort`.
5. Implement attach as `opencode attach <server_url> --session <provider_id>`.
6. Only after send + active phase + abort are proven, evaluate whether
   `steer_active_turn` should use async prompt, TUI prompt append/submit, or
   remain unsupported.
7. Advertise support bits only when the engine can actually start and control
   the server: `opencode.launch`, then `opencode.send`,
   `opencode.interrupt`, and optionally `opencode.steer`.

OpenCode should not use process-only `opencode_process` as a control plane once
the server sidecar exists. Introduce a new control plane such as
`opencode_server_bridge` rather than overloading the observe-only name.

### Antigravity

Antigravity has hooks and plugin installation today. Its hooks can observe
runtime phases and can inject steps at defined loop points. There is not
currently a Longhouse-owned live server/control path equivalent to Codex or
OpenCode in this repo.

Target Antigravity adapter:

1. Keep local launch through the stock `agy` binary and Longhouse plugin.
2. Add a durable local control inbox under the managed Antigravity runtime dir.
3. First implement `send_input` as next-invocation injection:
   - Longhouse writes a pending input to the local inbox.
   - `PreInvocation` returns `injectSteps: [{ userMessage: ... }]`.
   - If the agent reaches `Stop` while pending input exists, the hook can
     return `decision: "continue"` with a reason that triggers the next loop.
4. Do not mark `steer_active_turn` supported until we prove that an input can
   be injected into an already-active turn with bounded latency and explicit
   idle rejection.
5. Interrupt can start as a process signal only if the CLI handles it as a
   graceful stop. Otherwise keep it unsupported and expose terminate separately.
6. Add release canaries around hook schema, plugin install, transcript binding,
   pending input delivery, and stop/continue behavior.

Antigravity's control plane should be named for its real mechanism, for
example `antigravity_hook_inbox`, not `antigravity_process`.

## Shared Architecture Target

Add a provider adapter contract with these fields:

```text
provider
managed_transport
control_plane
binary_resolution
launch_modes
operation_support:
  launch_local
  launch_remote
  reattach
  send_input
  interrupt
  steer_active_turn
  terminate
runtime_sources
transcript_binding_sources
release_canary_profile
```

This should be a contract registry, not a polymorphic mega-class. Shared code
may ask the registry what a provider claims. Provider-specific code still owns
how to execute each operation.

Consumers:

- `ManagedSessionTransport.for_provider`
- `managed_local_launcher.record_connection`
- `managed_local_transport`
- `managed_control_dispatcher`
- kernel capability projection
- machine control `supports[]`
- local-health provider readiness
- Sauron release status artifacts

## E2E Contract For Each First-Class Provider

Before a provider is marked first-class, tests must prove:

1. Local attached launch creates a managed session and runtime events.
2. Detached/remote launch creates a live session without a visible terminal.
3. Reattach opens the provider UI against the same session.
4. `send_input` reaches the provider and persists a matching user transcript
   event or provider message record.
5. Explicit `intent=steer` succeeds only during fresh `thinking` or `running`.
6. Explicit `intent=steer` while idle returns `turn_not_active`.
7. `interrupt` stops or requests stop of an active turn.
8. Provider process exit becomes a terminal runtime signal without inventing
   false read-only state while the process is still live.
9. Local-health reports binary path, version, control path, runtime phase
   source, transcript binding, and advertised operations.
10. Release canary emits Green/Yellow/Red with raw evidence links.

## Immediate Delivery Order

1. Finish Claude remote launch on Machine Agent control channel.
2. Build OpenCode server-bridge sidecar and `opencode-channel send/interrupt`.
3. Add OpenCode remote launch and attach.
4. Decide OpenCode steer only after active-turn semantics are proven.
5. Build Antigravity hook inbox for queued/next-invocation input.
6. Decide Antigravity interrupt and steer only after hook canaries prove
   bounded behavior.
7. Move all provider operation truth into the contract registry and remove
   scattered provider-string gates.
