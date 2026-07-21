# Cursor Console Native Turns

Status: implementation locked
Owner: Longhouse Machine Agent + session kernel
Date: 2026-07-18
Related:

- `turn-scoped-console-execution.md`
- `console-turn-transcript-convergence.md`
- `cursor-helm-launch-parity.md`
- `cursor-storage-v2-source-fidelity.md`

## Decision

Cursor Console executes one stock native Cursor invocation per accepted turn:

```text
cursor-agent --print --output-format stream-json \
  --trust --resume <native-chat-id>
```

Fresh threads reserve their native identity with `cursor-agent create-chat`.
Later turns launch a new process against that same identity. The invocation
exits after the turn, while the Longhouse thread and Cursor conversation remain
durable. Console never owns an idle provider process.

`cursor_print` is independent of `cursor_helm`. Helm keeps the stock
interactive TUI attached to the user's terminal; Console uses Cursor's native
non-interactive output mode. Cursor ACP is a legacy archive source only and is
not a production execution fallback.

## Product Contract

Cursor Console supports:

- remote first and later turns on a selected user machine and workspace;
- native Cursor identity, `store.db` archive, search, and cold resume;
- structured live prose, reasoning, tool calls, results, and terminal state;
- bounded remote permission allow/deny, failing closed when unavailable;
- FIFO messages and process-group interruption of the active turn;
- Machine Agent and Runtime Host outage recovery without prompt replay.

It does not advertise active-turn steer or generic pause answering. A normal
message during active work queues the next turn.

## Invocation Contract

Before spawn, the Machine Agent durably claims the run and records the complete
Longhouse binding. It reserves the native Cursor conversation and acquires the
same per-conversation exclusivity used by Helm. Cursor stdout and stderr go
directly to private run files before any live projection reads them.

The claim records the provider identity, launch identity, PID, process group,
process-start identity, and run-file paths. Duplicate `session.turn.start`
commands return the existing claim and never execute the prompt twice.

The provider process is not killed merely because the Machine Agent restarts.
At startup the engine reconciles nonterminal claims:

- a matching live process is monitored from its durable output file;
- a successful Cursor result settles the turn completed;
- a persisted interrupt request plus process exit settles it cancelled;
- a missing process without terminal evidence settles it failed;
- PID or process-start ambiguity fails closed and is never replayed.

Interrupt first persists intent, then sends SIGINT to the exact invocation
process group. The engine drains durable output and cleans remaining children
before releasing the thread's execution owner.

## Identity And Transcript Convergence

The pending Cursor binding claim exists before `--print` starts, preventing an
empty native store from racing into a duplicate Shadow session. The adapter
promotes the claim only when Cursor's durable `system.init.session_id` matches
the reserved native identity plus the Longhouse session and launch identifiers.
Cursor hooks remain on the synchronous path only for permission decisions;
Console lifecycle and binding come from the already-durable stream so hook
latency cannot destabilize the provider connection.

Stream-json is the provisional live lane. Every raw line is durable locally
before projection and is keyed by run plus byte offset. Known records project
user input, assistant text, tool state, provider identity, usage, and terminal
state; unknown records remain raw evidence.

Do not enable Cursor's `--stream-partial-output` on the 2026.07.16 client.
That mode reconnects after a completed assistant response, duplicates output,
and can terminate with `WritableIterable is closed`. Plain stream-json still
streams thinking and tool lifecycle records, then emits the complete assistant
message and result reliably for fresh and resumed native conversations.

The native Cursor storage-v2 source remains canonical. The invocation binding
carries session, thread, turn, run, request, and provider identities through
the native-store receipt boundary. Live and durable records reconcile by
provider tool identity when available and by run-scoped ordinal otherwise.
Optimistic input is superseded by its exactly bound native user record, never
by text comparison.

Here, canonical means the lossless provider archive. It does not prove that
every persisted retry artifact was presented by an interactive TUI. That
separate visibility contract is defined in
`docs/specs/cursor-output-visibility-contract.md`.

## Capability And Cutover

The provider contract names `cursor_print` as Cursor's Console adapter and
advertises `cursor.turn_start` plus `cursor.turn_interrupt` only after the live
product canary passes. `launch_remote` and legacy `run_once` remain separate
operations and are not used to infer turn-scoped Console support.

Historical ACP sessions remain readable. They cannot be continued after
cutover and return an explicit legacy-adapter-unavailable result. The legacy
ACP raw reader may remain temporarily to drain already-created local evidence,
but no new ACP process or source is created.

There is no implicit fallback to ACP or a detached TUI. An unsupported Cursor
version or unhealthy hook/store path makes the Console adapter unavailable.

## Release Gate

The real stock-Cursor product canary must prove fresh launch, second-process
resume, readable live and durable tools, allow and deny, interrupt during model
work and tool work, no orphan process, successful post-cancel resume, duplicate
dispatch safety, Machine Agent restart, Runtime Host outage recovery, FIFO
turns, cold reopen, and search. Web, iOS, CLI, local health, and
`/api/agents/*` must project the same capability truth.
