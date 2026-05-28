# Managed Provider Control Matrix

Status: Draft
Owner: Machine Agent + managed provider CLI surfaces
Updated: 2026-05-27
Related: `provider-cli-contracts-and-codex-release-canaries.md`, `managed-provider-live-canary-roadmap.md`, `remote-session-launch.md`, `machine-agent-control-channel.md`, `managed-session-stall-recovery.md`

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

First-class is a target capability class, not a transport shape. Codex reaches
it through an app-server bridge; Claude reaches it through the native channel
bridge. Do not describe Claude as a lower tier just because it does not use the
Codex transport. The separate question is whether each promised operation has
hermetic, no-token live, manual live-token, or scheduled live-token proof.

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

This table describes what Longhouse has wired today, not the ceiling of what
the upstream provider can support.

| Provider | Local Launch | Remote Launch | Send | Interrupt | Steer | Runtime | Transcript | Current Truth |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex | Yes, `longhouse codex` | Yes, engine `session.launch` | Yes, engine channel | Yes, engine channel | Yes, engine channel with active-turn errors | Bridge/runtime events | Hooks + rollout | First-class |
| Claude | Yes, `longhouse claude` | Yes, Machine Agent `claude.launch` with PTY-backed development-channel handshake | Yes, `claude-channel send` | Yes, `claude-channel interrupt` | Yes, dispatch gated by fresh active runtime phase and delivered through channel metadata; scheduled live-token canary still has to prove upstream mid-turn behavior continuously | Channel/hooks/process scan | Claude channel + transcript ingest | First-class channel control |
| OpenCode | Yes, `longhouse opencode` server bridge + `opencode attach` | Yes, Machine Agent `opencode.launch` when `opencode` is on PATH | Yes, server `prompt_async` API | Yes, server `abort` API | No, active-turn injection not proven | OpenCode plugin runtime events | Plugin/transcript observation | First-class launch/send/interrupt; no steer |
| Antigravity | Yes, `longhouse antigravity` / `longhouse agy` | No | Yes, hook inbox claimed by active hooks | No | No | JSON hooks + runtime outbox | Hook binding to transcript path | Send-only hook-inbox wrapper |

## Target Matrix

This is the launch target. If a provider cannot support a capability after
source-level canaries prove the upstream contract, the target row must be
downgraded with the failed evidence attached.

| Provider | Target Tier | Local Launch | Remote Launch | Send | Interrupt | Steer | Reattach | Release Drift Guard |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex | First-class | Attached + detached-ui managed bridge | Machine Agent `session.launch` | Engine channel | Engine channel | Engine channel, active-turn gated | `codex --remote` attach | Codex source/API canary |
| Claude | First-class | `longhouse claude` channel launch | Machine Agent `claude.launch` | Claude channel | Claude channel | Claude channel, active-turn gated | Channel resume/attach path | Claude channel/hook canary |
| OpenCode | First-class if server API remains stable | OpenCode server bridge | Machine Agent `opencode.launch` | Server prompt API | Server abort API | Only after async/TUI prompt semantics prove active-turn delivery | `opencode attach` | OpenCode OpenAPI + live server canary |
| Antigravity | Controlled wrapper with explicit limits | Stock `agy` + Longhouse plugin | Only after launch semantics are proven | Hook inbox / next-invocation injection | Only if graceful stop is proven | Only if active-turn injection is proven | Provider-supported reattach if exposed | Hook schema + inbox canary |

`scripts/qa/provider-release-profile-canary.py` now emits the shared
Sauron-facing release artifact for every managed provider. Provider-specific
release profile live fields remain yellow/not-run until real upstream provider
probes run. `scripts/qa/provider-control-e2e-canary.py` is the hermetic
Longhouse control-path canary: it proves the local commands and control
contracts without spending model tokens.

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

Steer dispatch is now a first-class Longhouse operation, but with an explicit
active runtime gate before dispatch. Longhouse does not claim that idle channel
injection is steer, and the scheduled live-token canary owns continuous proof
that upstream Claude treats channel delivery as mid-turn guidance rather than
queued next-turn input. If runtime phase is stale or idle, `intent=steer`
returns `turn_not_active`.

Next Claude gaps:

1. Extend the no-token live canary into detached channel launch readiness. The
   current live lane proves binary/auth/flag/channel-parser shape without
   starting a model turn.
2. Add a token-spending or controlled live canary for active-turn steer
   injection to scheduled CI/Sauron. The operator POC supports delayed
   `intent=steer` channel injection and transcript assertion, while the
   API/runtime tests prove idle steer rejection and active-phase dispatch
   gating.
3. Dogfood detached launch on Linux; macOS requires a `script(1)` PTY wrapper
   because stock Claude falls into print-mode behavior without a terminal.
   Hook tokens are passed through process env, not argv or PTY log text.

### OpenCode

OpenCode now uses a server bridge because stock OpenCode exposes a local HTTP
server model. The CLI exposes `opencode serve` and `opencode attach`; the local
server's `/doc` OpenAPI payload exposes session create/list, prompt, async
prompt, wait, abort, and TUI prompt append/submit endpoints.

Current OpenCode adapter:

1. Launch an engine-owned OpenCode server sidecar:
   - stock `opencode serve --hostname 127.0.0.1 --port 0`
   - `OPENCODE_CONFIG_CONTENT` includes the Longhouse runtime plugin
   - state file records `session_id`, `server_url`, pid, auth, cwd, and
     provider session id with mode 0600
   - launch is idempotent per Longhouse session id and guarded by a lock
2. Create or resolve the OpenCode session through the server API.
3. Implement `longhouse opencode-channel send` against
   `/session/:id/prompt_async`; the no-token live canary now proves noReply
   user-message delivery by reading the marker back from
   `/session/:id/message`.
4. Implement interrupt against `/session/:id/abort`.
5. Implement attach as `opencode attach <server_url> --session <provider_id>`.
6. Only after assistant response execution + active phase + abort are proven,
   evaluate whether `steer_active_turn` should use async prompt, TUI prompt
   append/submit, or remain unsupported.
7. Advertise support bits only when the engine can actually start and control
   the server: `opencode.launch`, `opencode.send`, and
   `opencode.interrupt`. Do not advertise `opencode.steer` yet.

OpenCode should not use process-only `opencode_process` as a control plane once
the server sidecar exists. Introduce a new control plane such as
`opencode_server_bridge` rather than overloading the observe-only name.

### Antigravity

Antigravity has hooks and plugin installation today. Its hooks can observe
runtime phases and can inject steps at defined loop points. Longhouse now uses
that hook surface as a send-only inbox while an Antigravity loop is alive. The
hook inbox is the only Longhouse-owned Antigravity control surface today; there
is no live server bridge equivalent to Codex or OpenCode in this repo.

Target Antigravity adapter:

1. Keep local launch through the stock `agy` binary and Longhouse plugin.
2. Maintain a durable local control inbox under the managed Antigravity runtime
   dir.
3. Implement `send_input` as next-invocation injection:
   - Longhouse writes a pending input to the local inbox.
   - `PreInvocation` returns `injectSteps: [{ userMessage: ... }]`.
   - `PostInvocation` also claims pending input and returns
     `terminationBehavior: "force_continue"` when it injects a message.
   - If the agent reaches `Stop` while pending input exists, the hook returns
     `decision: "continue"` with a reason that triggers the next loop.
   - The hermetic control-path canary proves the generated hook claims queued
     input at `PreInvocation` and `PostInvocation`, requests
     `force_continue`, and continues at `Stop` when inbox input is waiting.
     A real upstream `agy` canary still needs to prove provider release drift.
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

Provider contract facts live in
`server/zerg/config/managed_provider_contracts.json`. Python reads that
manifest for managed-provider contracts and provider CLI discovery; the Rust
Machine Agent embeds the same manifest for `supports[]` advertisement. Provider
execution remains provider-specific code.

The manifest is intentionally two-axis:

- operation booleans describe what Longhouse intends and implements for that
  provider
- `operation_evidence` describes how strongly each operation has been proven
  today

That separation prevents false downgrades like "Claude is 1.5 because it uses
channels" and false upgrades like "OpenCode steer is supported because send
works while a process is busy."

The manifest carries these fields:

```text
provider
provider_cli_binary
provider_cli_env
requires_longhouse_cli
managed_transport
control_plane
control_plane_aliases
launch_local
launch_remote
reattach
send_input
interrupt
steer_active_turn
terminate
tail_output
runtime_phase
transcript_binding
can_resume
operation_evidence
machine_control_supports
```

This is a contract registry, not a polymorphic mega-class. Shared code
may ask the registry what a provider claims. Provider-specific code still owns
how to execute each operation.

Consumers:

- `ManagedSessionTransport.for_provider`
- `managed_local_launcher.record_connection`
- `managed_local_transport`
- `managed_control_dispatcher`
- kernel capability projection
- machine control `supports[]`
- local-health provider readiness and `control_operations_by_provider`
- Sauron release status artifacts for every managed provider

## E2E Contract For Each First-Class Provider

Before a first-class provider target is marked Green/release-ready, tests must
prove:

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

1. Finish Claude remote launch on Machine Agent control channel. Done.
2. Build OpenCode server-bridge sidecar and `opencode-channel send/interrupt`. Done.
3. Add OpenCode remote launch and attach. Done.
4. Decide OpenCode steer only after active-turn semantics are proven.
5. Build Antigravity hook inbox for queued input claimed by active hooks. Done.
6. Decide Antigravity interrupt and steer only after hook canaries prove
   bounded behavior.
7. Move all provider operation truth into the contract registry and remove
   scattered provider-string gates. Python read surfaces and Rust support
   advertisement now share the manifest; provider-specific launch/dispatch code
   still intentionally branches per provider.
