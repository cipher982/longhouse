# Cursor And OpenCode Console Launch Parity

Status: implemented; release evidence recorded below
Owner: Longhouse Machine Agent + session kernel
Date: 2026-07-18
Related:

- `turn-scoped-console-execution.md`
- `console-turn-transcript-convergence.md`
- `cursor-console-native-turns.md`
- `managed-provider-session-contract.md`

## Outcome

Cursor and OpenCode are launch-ready Console providers without claiming
active-turn steering.

For either provider, a user can select a connected machine and workspace in
Longhouse web or iOS, start a fresh task, watch prose and tools live, interrupt
the active invocation, send a later message that resumes the same native
provider thread, and recover cleanly across Machine Agent or Runtime Host
restarts. An idle Console thread owns durable identity, not an idle provider
process.

The product matrix after this work is:

| Capability | Cursor | OpenCode |
| --- | --- | --- |
| Shadow live/archive | yes | yes |
| Helm terminal control | yes | yes |
| Console fresh turn | yes | yes |
| Console native resume | yes | yes |
| Console live prose/tools | yes | yes |
| Console interrupt | yes | yes |
| Console restart recovery | yes | yes |
| Active-turn steer | no | no |

## Shared Turn Contract

A Console turn is one accepted user message followed by provider reasoning,
tools, assistant output, and one terminal outcome. The Runtime Host creates the
durable input, turn, and run before dispatch. The Machine Agent receives
`session.turn.start`, durably claims the run before spawn, and returns an
idempotent acknowledgement. The provider invocation exits after the turn.

Each provider adapter must implement the same lifecycle:

1. validate provider binary, workspace, launch permission mode, and resume identity;
2. claim the run and provider thread before spawning;
3. write raw stdout and stderr to private durable files;
4. record PID, process group, process-start identity, native thread identity,
   launch identity, and run-file paths;
5. project structured live events keyed by run and source offset;
6. settle exactly one completed, failed, or cancelled terminal state after a
   bounded output drain;
7. recover a matching live process after Machine Agent restart without prompt
   replay;
8. reject ambiguous ownership, identity, or terminal evidence rather than
   launching a duplicate.

`session.turn.interrupt` persists cancellation intent, targets only the exact
active run and process group, drains output, and leaves the durable provider
thread resumable. Messages accepted during an active turn remain FIFO queued
for the next turn. Neither provider advertises steer or pause-answer.

## Cursor Track

Cursor continues to use the implemented `cursor_print` adapter:

```text
cursor-agent --print --output-format stream-json \
  --trust --resume <native-chat-id>
```

No alternate Cursor execution design is introduced. The implementation closes
the release gates named by `cursor-console-native-turns.md`:

Stock Cursor 2026.07.16 preserves the native chat identifier across
`--resume`, but its print-mode invocation does not restore earlier turns to the
model context. Longhouse therefore reconstructs a bounded continuation context
from the prior durable Console run records while still passing the same native
chat ID. This is compatibility rehydration, not a second conversation store:
the raw run records are already required for projection recovery, and Cursor's
native storage remains the archive convergence source.

- exercise the production Runtime Host -> Machine Agent -> Cursor path rather
  than only the adapter boundary;
- prove structured tool projection, permission allow and deny, model failure,
  active-turn interruption, post-cancel resume, FIFO dispatch, duplicate
  command idempotency, and no orphan child process;
- prove Machine Agent restart monitoring and Runtime Host disconnect/reconnect
  without replay;
- prove native storage-v2 convergence, cold reopen, and search under the same
  Longhouse session and thread identity;
- keep web, iOS, CLI, local health, machine directory, and `/api/agents/*`
  capability truth aligned.

Cursor does not gain a continuously idle provider process. Durable detached
runtime means the thread can be resumed after process and machine-agent
turnover, not that Cursor remains running between turns.

## OpenCode Track

OpenCode gains a production `opencode_run` turn adapter using the user's stock
binary:

```text
opencode run --format json --auto \
  [--session <native-session-id>] <prompt>
```

Native OpenCode identities are opaque `ses_...` strings, not UUIDs. Resume uses
only explicit `--session <id>`. Console never uses `--continue` (ambient last
session) or `--attach` (server/TUI semantics). The event schema and help-visible
flags are guarded by a real-provider canary. Longhouse does not vendor or patch
OpenCode.

OpenCode Console launch initially supports explicit bypass only through stock
`--auto`. Cursor's hook-backed `remote_approve` behavior is not inferred for
OpenCode. If bypass is not selected, launch fails with a typed unsupported
permission-mode result rather than waiting on an invisible interactive prompt.

The adapter reuses the shared durable turn-claim registry and process-identity
rules, but owns an OpenCode-specific parser and native-session binding. It must:

- extract exactly one native session identity from structured output on a
  fresh turn and persist it before terminal settlement;
- require that identity for later turns and reject mismatches;
- project text plus tool pending/running/completed/error records with stable
  provider call IDs;
- preserve every unknown JSONL record as raw evidence;
- use a private, run-scoped provider config and Longhouse runtime plugin so
  execution cannot collide with a Helm bridge or leak tokens;
- implement exact process-group interrupt and startup recovery;
- avoid creating a duplicate Shadow session while the Console binding is
  pending or active.

A fresh OpenCode turn cannot know its `ses_...` identity before spawn. A pending
binding keyed by Longhouse run/session/launch identity must therefore suppress
Shadow duplication until the first structured event supplies `sessionID`; that
event atomically promotes the native binding. A turn that completes without one
native session identity fails typed and cannot be resumed.

After the installed-provider canary passes, the provider contract advertises
`opencode.turn_start` and `opencode.turn_interrupt`. The Machine Agent dispatch
table, machine directory, Console launcher, session capability projection, web,
and iOS consume those contract bits rather than provider-name allowlists.

The existing OpenCode Helm bridge remains separate. Console does not silently
fall back to `opencode serve`, `opencode attach`, generic Runner execution, or
the legacy `session.run_once` command.

## Capability Truth And Failure UX

A provider appears in the Console launcher only when the selected machine
advertises its turn-start bit. The Machine Agent advertises that bit only when
the provider binary is on PATH and its contract/canary lane supports the
installed CLI shape; there is no claim that every launch performs a live model
health probe.
Interrupt appears only while an exact nonterminal run advertises the matching
turn-interrupt bit. Missing binary, unsupported version, unavailable workspace,
invalid native resume identity, permission failure, and ambiguous recovery are
typed failures, not generic send errors.

The public product vocabulary remains Console. `turn_start`, adapter names,
claims, and process groups are implementation details.

## Regression Harness

The harness has three layers and uses production boundaries:

1. **Hermetic engine contract tests** use fake provider executables to prove
   argv/env, structured event projection, tool lifecycle, native identity,
   duplicate dispatch, FIFO blocking, interrupt targeting, output drain,
   terminal settlement, PID-reuse rejection, and restart recovery.
2. **Runtime Host-to-Machine-Agent integration tests** exercise the HTTP Console
   launch and interrupt routes through an actual control-channel frame and a
   Machine Agent running a fake provider binary. Fake registries alone are unit
   coverage, not this boundary. Tests assert durable input/turn/run state,
   capability gating, live event identity, transcript convergence, resume
   identity, and typed failure behavior for both providers.
3. **Installed-provider canaries** spend real provider turns against stock
   Cursor and OpenCode. A provider CLI contract canary first proves help/argv,
   JSONL, tool shape, and native resume. The release canary must additionally
   traverse Runtime Host -> control channel -> claim -> adapter; a bare CLI or
   direct adapter test is not Longhouse Console proof. Together they prove a
   fresh text turn, a structured tool turn, a second-process native resume,
   active-turn interruption, successful post-cancel resume, no orphan process,
   and archived/searchable convergence.

Daily/pre-merge CI runs hermetic and Runtime Host integration layers. The live
provider lane is explicit, credential-aware, produces hashed evidence artifacts,
and fails release promotion without silently skipping an installed provider.
Provider-version fixture corpora retain raw JSONL samples so parser regressions
can be reproduced without spending model turns.

Required Make targets:

```text
make test-engine
make test
make test-frontend
make test-e2e-core
make test-cursor-console-live-canary
make test-cursor-console-product-e2e
make test-opencode-console-live-canary
make test-opencode-console-product-e2e
```

The Cursor live canary must ask for information present only in the previous
turn. Repeating the expected marker in the follow-up prompt is not continuity
proof. The OpenCode product canary traverses the hosted Runtime Host control
channel and additionally proves archive/search convergence; this shared
product boundary is provider-neutral while each real-provider canary proves
its adapter-specific CLI and process behavior.

## Formal Task List

### A. CLI truth and narrow shared lifecycle

- [x] Add a stock OpenCode CLI contract canary proving `--auto`, fresh `ses_...`
      identity, explicit `--session` second-process resume, tool JSONL, process-
      group interrupt, and post-cancel resume. Assert that `--continue`,
      `--attach`, and the obsolete permission flag never enter Console argv.
- [x] Persist the last projected stdout byte offset (and parser sequence when
      needed) in durable turn claims so Machine Agent recovery cannot re-emit
      already projected records. Apply the same invariant to Cursor.
- [x] Add a small adapter-selected startup recovery seam; retain an explicit
      provider-to-adapter match rather than building a generic framework.
- [x] Ensure Runtime Host, machine directory, web, and iOS derive launch and
      interrupt affordances from the same provider contract.
- [x] Add cross-provider contract tests preventing advertised operations from
      lacking an engine dispatch path.

### B. Cursor release hardening

- [x] Fill the Cursor release-gate cases missing from the current adapter and
      canary: tools, allow/deny, failure, interrupt, post-cancel resume,
      duplicate dispatch, FIFO, restarts, outage, cold reopen, and search.
- [x] Fix any failures found without changing the stock Cursor invocation or
      introducing an execution fallback.
- [x] Promote Cursor evidence metadata only to the level actually proven.

### C. OpenCode turn adapter

- [x] Implement `opencode_run` in the Machine Agent with stock-binary discovery,
      private config, durable raw output, and structured JSONL projection.
- [x] Accept opaque `ses_...` native identities, require explicit `--session`
      for resume, scrub ambient OpenCode configuration, and support only the
      explicit launch-bypass permission mode via `--auto`.
- [x] Bind fresh and resumed native OpenCode session IDs to the Longhouse
      session/thread/run before archive ingestion can create a duplicate.
- [x] Add exact interrupt, output drain, terminal settlement, orphan cleanup,
      and Machine Agent restart recovery.
- [x] Route `session.turn.start` and `session.turn.interrupt` to OpenCode and
      advertise the two support bits only after the production canary passes.

### D. Product surfaces

- [x] Enable OpenCode in Console machine/provider selection when capability is
      present and show typed unavailability when it is absent.
- [x] Verify fresh launch, active output, stop, queued follow-up, resumed turn,
      and terminal state in web and iOS against the same API contract.
- [x] Keep unsupported steer absent rather than disabled ambiguously.

### E. CI and release proof

- [x] Add shared fake-provider fixtures and golden JSONL corpora for Cursor and
      OpenCode.
- [x] Add Runtime Host-to-Machine-Agent integration coverage using a real
      command frame plus fake provider process for both provider lifecycles;
      keep fake-registry tests as the unit layer.
- [x] Add `make test-opencode-console-live-canary` and evolve the Cursor target
      to the complete release gate.
- [x] Run focused suites during implementation, then the required Make targets;
      record any unrelated baseline failures explicitly.

## Release Evidence

Both provider product canaries passed against stock installed CLIs on `cinder`.
Each traversed the hosted Runtime Host control channel, created a fresh turn,
recalled a marker that appeared only in the prior turn, used the same explicit
native provider identity, began a real tool call, interrupted its exact process
group, resumed after cancellation, converged into the archive, and became
searchable. Cursor additionally exposed and fixed a missing Runtime Host
binding signal; without this end-to-end test, its adapter-only resume test would
not have caught the catalog assigning a new native identity.

## Definition Of Done

This work is complete only when both providers can be selected in Console on a
capable machine and the same user-visible sequence passes through the production
surface: fresh prompt, live prose/tool output, interrupt, post-interrupt prompt,
native resumed identity, terminal settlement, cold reopen, and search. Restart
and duplicate-delivery tests must prove no prompt replay and no orphan provider
process. Capability metadata, UI affordances, and installed-provider evidence
must agree. Steering remains explicitly unsupported.
