# Cursor Helm Launch Parity

Status: proposed — evidence gates must pass before capability promotion
Owner: Longhouse Machine Agent + provider launchers
Last updated: 2026-07-17
Supersedes the Cursor Helm product conclusions in
`cursor-transcript-format.md`, `cursor-storage-v2-source-fidelity.md`, and
`capability-gated-degraded-helm.md` where they conflict with this document.

## Decision

Cursor is launch-ready as a Helm provider only when Longhouse can prove the
same user outcomes it promises for Claude and Codex:

1. the stock interactive Cursor TUI remains the user's terminal UI;
2. the managed Longhouse session and Cursor's durable conversation have one
   stable identity;
3. the transcript is visible live, durable, searchable, and reconstructable;
4. remote send, active-turn steer, graceful turn cancellation, permission
   response, resume, and control-path recovery have truthful semantics;
5. a Longhouse failure never terminates or corrupts provider execution.

Provider-specific mechanics may differ. Product semantics may not. A control
operation is unavailable until a real Cursor process proves it. Hermetic tests
protect mechanics but do not promote an unsupported provider behavior.

If stock Cursor cannot satisfy one of these launch requirements without
Longhouse replacing its TUI or silently changing execution mode, Cursor Helm
does not ship as a launch-ready provider. Cursor Shadow and Console remain
separate capabilities; neither may be presented as Helm parity.

## Current Truth

The current `cursor_helm` control plane is a PTY owner around stock
`cursor-agent`:

- send writes text, waits, sends Escape, then Enter;
- interrupt sends `SIGINT`, which exits the provider process in current proof;
- terminate sends `SIGKILL`;
- active-turn steer, pause-answer, reattach, and resume are not implemented;
- provider activity is not observed, so the host may project idle while Cursor
  is working;
- the Helm session is control-only and has no transcript binding.

The native Cursor storage-v2 source separately captures `store.db` bytes under
a deterministic Shadow session derived from Cursor `agentId`. It emits
`render: null`, so the durable raw session remains unreadable and hidden from
the default timeline. Cursor ACP Console has its own durable source and must
not lend transcript capability bits to `cursor_helm`.

This is not Claude/Codex parity. Public copy and per-session capabilities must
say so until the acceptance gates below pass.

## Provider Evidence To Prove

The installed Cursor 2026.07.13 and 2026.07.16 binaries contain a hidden
`--new-session-id <uuid>` option described as creating a session with a
caller-provided ID. Cursor hooks expose `conversation_id`, lifecycle/tool
events, turn-boundary assistant text, and reasoning text. The Longhouse
launcher already injects `LONGHOUSE_SESSION_ID` into the Cursor child
environment. Cursor also exposes `create-chat`, `--resume`, `--continue`,
stream-json headless output, and ACP.

None of those observations is sufficient by itself. The integration must
capture live evidence from the exact supported Cursor binary and account:

| Proof | Required observation |
| --- | --- |
| Caller identity | Both `create-chat` → `--resume <id>` and `--new-session-id <uuid>` are tested; the selected path provides an ID before TUI launch and that ID matches `meta['0'].agentId` plus hook `conversation_id`. |
| Hook provenance | A hook invoked by that TUI inherits the exact `LONGHOUSE_SESSION_ID`, reports the provider conversation ID, and cannot cross-bind a concurrent session. |
| Transcript | User, assistant, reasoning, tool start, and tool result become visible in provider order; exact raw store evidence remains receipt-backed. |
| Idle send | Remote text creates exactly one new user turn and one provider response. |
| Active steer | A message accepted during an active turn changes that same turn without starting a second turn or restarting Cursor. |
| Graceful cancel | Cancel stops the active turn, preserves the TUI and conversation, and accepts the next prompt. |
| Permission response | A remote allow/deny response resolves the exact pending request once. |
| Resume | A stopped conversation resumes by native Cursor ID with its durable history and the same Longhouse session identity. |
| Recovery | Restarting the Machine Agent/Runtime Host loses no provider execution and restores every capability that has provider proof. |

The proof harness stores sanitized JSON evidence: provider version, Longhouse
build, requested ID, observed hook ID, observed store ID, process identities,
operation timestamps, terminal outcome, and transcript assertions. It never
stores credentials or relies on screen-text classification when a provider
event exists.

## Architecture Under Test

### Identity and binding

For a fresh Helm launch, Longhouse first tries the strongest provider identity
path proven by Gate 0:

1. provider-minted identity from `create-chat`, followed by
   `--resume <provider-id>`; or
2. Longhouse-minted identity passed through the hidden
   `--new-session-id <uuid>` compatibility path.

The documented/provider-minted path wins when both are reliable. The hidden
flag is never assumed merely because binary strings contain it. A Longhouse
Cursor hook records a binding claim from two independent values:

- `LONGHOUSE_SESSION_ID` inherited from the exact launched process;
- Cursor `conversation_id` from the provider hook payload.

The Machine Agent accepts a binding only when the hook and native store agree
on Cursor identity and the claim matches the live launch process. Time, cwd,
workspace hash, newest-file selection, and process recency are diagnostics,
never binding evidence. Unsupported provider versions fail closed to an
explicit control-only state.

### Live and durable transcript

Cursor hooks provide provisional phase, prompt, tool, assistant-response, and
reasoning evidence at provider hook boundaries. They are not a token-delta
stream. The native `store.db` adapter remains the durable source of truth and
must poll fast enough to meet the live readability contract, emitting a
versioned render projection as well as exact raw records. Durable records
reconcile provisional records by provider identity and order. Unknown Cursor
blocks remain raw evidence with typed render gaps.

The managed binding makes the native source use the Helm session ID. It must
not create a second hidden Shadow session for the same conversation. Binding
alone never unhides a session: the timeline may expose the bound session only
after its first readable render generation is committed.

### Hook lifecycle and permission safety

Longhouse installs one user-level Cursor hook adapter through the normal
onboarding/`machine repair` integration step. Installation merges by Longhouse
identity, preserves unrelated hooks and ordering, records what it owns, and
supports idempotent upgrade and uninstall. A missing, rejected, or unsupported
hook is an explicit health/capability failure; it never blocks the stock TUI.

Permission hooks may wait for a bounded remote answer. If Longhouse is
unavailable or the wait expires, the adapter returns control to Cursor's local
permission prompt. It never silently allows, denies, or leaves the TUI hung.

### Control

Use the strongest stock Cursor mechanism that passes each proof:

1. documented hook or protocol operation;
2. stable native CLI operation with a versioned compatibility canary;
3. PTY key sequences only when they map to a proven stock-TUI operation and a
   hook/store event confirms the semantic result. This includes idle prompt
   submission and may include Escape cancellation if the live canary proves it
   stops only the active turn.

Do not call queued input active steer. Do not call process exit interrupt. Do
not infer permission state from terminal pixels if a hook event can represent
it. The current send sequence includes Escape; without a provider-phase idle
guard, a remote send can cancel active work. That guard is mandatory before
send remains advertised. ACP and the Cursor SDK may improve Console, but replacing the interactive
TUI changes the mode and cannot be used to claim Helm parity.

### Resume and recovery

Persist the provider conversation ID and native resume command as part of the
managed session contract. Resume starts stock Cursor against that identity and
re-establishes a fresh control lease for the same Longhouse session. Recovery
of Longhouse-owned sockets, hooks, or claims may never signal the provider.

## Capability Projection

Capabilities are per control plane and current proof, not provider-wide:

- `cursor_helm` receives only operations proven for the live PTY/hook session;
- `cursor_acp` receives only operations proven for Console ACP;
- `cursor_store` is durable/search-only only after a render generation exists.

`can_tail_output` requires readable current output, not merely durable raw
bytes. `can_interrupt` requires graceful active-turn cancellation.
`can_resume` requires a tested native resume path. Active-turn steer and
permission response remain false until their individual canaries pass.

## Implementation Slices

1. **Provider proof harness:** exercise both identity paths, hooks, concurrent isolation,
   transcript, send, cancel, permission, resume, and recovery against the real
   installed Cursor binary. Keep sanitized fixtures for CI replay.
2. **Honesty baseline:** split Cursor control-plane capability projection,
   remove process-exit interrupt and unrendered tail claims, and correct
   launcher, README, docs, web, and iOS copy.
3. **Identity:** launch with the proven pre-known provider ID, install the hook adapter,
   persist strict claims, and bind storage-v2 to Helm without duplication.
4. **Transcript:** emit Cursor render records, stream provisional hook events,
   reconcile them with durable receipts, and expose typed render gaps.
5. **Control:** promote only operations with successful live proofs; guard idle
   PTY send with provider phase and remove misleading interrupt semantics.
6. **Resume/recovery:** implement native resume and prove Machine Agent and
   Runtime Host restart behavior without provider termination.
7. **Product verification:** web/iOS session detail, timeline, search, CLI,
   local-health, and machine API all agree on the same session and capability
   state.

Each slice is an atomic commit with focused tests. Do not defer the real Cursor
canary to the end; every promoted operation lands with its proof.

## Required Tests

### Hermetic

- caller UUID argv construction and version-gated fallback;
- hook payload validation, environment/session matching, expiry, replay, and
  concurrent-session isolation;
- native store identity agreement and no duplicate Shadow session;
- Cursor raw-to-render fixtures for text, reasoning, tool calls/results,
  unknown blocks, WAL growth, rewrites, and restart;
- provisional-to-durable reconciliation and idempotency;
- per-control-plane capability projection;
- send while active is rejected before any PTY byte is written;
- a permission-hook timeout returns to Cursor's local prompt without allow or
  deny;
- a bound raw source remains hidden until a render generation exists;
- no `cursor_helm` capability cites `cursor_acp` evidence;
- no provider signal from repair, cleanup, or Longhouse restart.

### Real provider integration

- `create-chat`/`--resume` and caller-ID round trips plus hook binding;
- one idle remote send observed in provider output and durable transcript;
- active-turn steer canary;
- graceful cancel followed by a successful next turn;
- permission allow, deny, and Longhouse-unavailable timeout canaries;
- send attempted mid-turn does not cancel or corrupt the active turn;
- exit, native resume, and next-turn continuity;
- Machine Agent restart and Runtime Host outage/recovery while the TUI lives;
- two concurrent Cursor Helm sessions in the same cwd never cross-bind.

CI replays sanitized provider evidence. Release qualification runs the live
provider canary against the exact supported Cursor version. A version mismatch
cannot reuse an older green result.

## Promotion Bar

Cursor Helm is launch-ready only when:

- every launch requirement has a green live-provider artifact;
- first provisional output is terminal-class and durable readable transcript
  converges within the product's ten-second p95 contract;
- browser, iOS, CLI, local-health, and `/api/agents/*` agree;
- concurrent and restart tests prove identity and provider survival;
- no capability is borrowed from Cursor Console or inferred from provider
  type;
- public docs describe the observed behavior without caveats hidden in
  terminal warnings.

If stock Cursor cannot prove active steer, graceful cancel, permission
response, or recovery, record the exact upstream gap and keep Cursor Helm out
of the launch-ready provider set. Do not weaken this bar by renaming PTY
behavior.
