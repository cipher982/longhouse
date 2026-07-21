# Claude Console Plan

Status: proposed implementation plan; no behavior shipped
Owner: Longhouse Machine Agent + session kernel
Date: 2026-07-21
Related:

- `ARCHITECTURE.md`
- `docs/specs/turn-scoped-console-execution.md`
- `docs/specs/console-turn-transcript-convergence.md`
- `docs/specs/session-mode-legibility-epic.md`
- `docs/specs/managed-session-state-normalization-epic.md`
- `docs/specs/durable-shipping-resilience.md`

## Decision

Build `claude_print` as Claude's `session.turn.start` Console adapter.

Do not build Cursor remote Helm. Cursor already has terminal-originated Helm
through `longhouse cursor` and turn-scoped Console through `cursor_print`.
A persistent no-terminal Cursor process would recreate the invalid
`session.launch` abstraction without adding an upstream control capability.

Do not preserve or extend provider-facing `session.launch`. The existing
persistent no-terminal Claude/Codex/OpenCode launch paths are separate deletion
work tracked by `docs/specs/turn-scoped-console-execution.md` and the
`session-launch-removal` docket item. Claude Console must use one invocation
per turn and must not reuse the persistent Claude channel launcher.

The adapter-scoped provider schema remains desirable, but it is not a
prerequisite for Claude Console. The current schema can promote
`console_adapter: claude_print` and `turn_start: true` after live proof.

## Current Capability Truth

| Provider | Terminal-originated Helm | Console `turn_start` | Active-turn steer |
| --- | --- | --- | --- |
| Claude | shipped through `longhouse claude` | missing adapter | Helm only |
| Codex | shipped through `longhouse codex` | shipped through `codex_exec` | Helm only |
| Cursor | shipped through `longhouse cursor` | shipped through `cursor_print` | unsupported upstream |
| OpenCode | shipped through `longhouse opencode` | shipped through `opencode_run` | unsupported upstream |

Remote clients may steer an existing Helm session when its adapter proves the
operation. They do not originate Helm sessions. Web/iOS **New Session** creates
an empty Console thread and the composer dispatches turn-scoped invocations.

## Evidence Reviewed

The original investigation checked:

- the authored provider source `schemas/managed_providers.yml` and generated
  `server/zerg/config/managed_provider_contracts.json`;
- production dispatch in `engine/src/control_channel.rs`;
- current Console implementations in `engine/src/codex_exec.rs`,
  `engine/src/cursor_print.rs`, and `engine/src/opencode_run.rs`;
- Claude lifecycle hooks and storage-v2 transcript binding;
- installed Claude 2.1.198 help and official headless/session documentation;
  and
- a live two-invocation Claude probe using one explicit session UUID.

The live probe emitted parseable stream JSON, a matching `system.init` session
ID, assistant records, and a final `result` record. Each invocation exited, and
a second process using `--resume` restored the same provider conversation.
This proves CLI feasibility, not Longhouse product readiness.

## Dormant `companion-claude-print` Worktree

The dormant worktree is on `worktree-companion-claude-print`; committed WIP is
`33527a10e` (`WIP: claude_print adapter`). It is a reference implementation,
not a branch to merge or rebase.

### Reusable design

- one `claude --print --output-format stream-json --verbose` process per turn;
- caller-minted provider UUID through `--session-id` for the first turn and
  `--resume` later;
- private stdout/stderr run files;
- a per-provider-session execution lock;
- process-group identity, interrupt, cleanup, durable turn claims, restart
  monitoring, and projection checkpoints;
- raw structured-event retention plus phase, binding, progress, and terminal
  projections; and
- recovery from a live claimed process or already-durable stdout without
  replaying the prompt.

### Prototype defects that must not be ported

- It never sets `LONGHOUSE_MANAGED_SESSION_ID`; Claude's lifecycle hook can
  therefore ingest the native transcript as a duplicate Shadow session.
- It writes `managed_session_state` through a path current main removed.
- It treats process exit as success without requiring a durable successful
  provider `result` record.
- Its `control_channel.rs` changes predate current permissions, dispatch parity,
  and Console turn behavior.
- It edits generated JSON instead of the authored YAML.
- Its private binding-probe tree is not Claude's canonical storage-v2 binding
  authority.

## Target Design

Add `engine/src/claude_print.rs` beside `cursor_print.rs` and
`opencode_run.rs`, following their current shared conventions:

1. `execute_turn_start` durably claims `run_id` before adapter selection.
2. The adapter chooses or validates the provider UUID and takes a nonblocking
   per-provider-session execution lock.
3. Before spawn it creates private stdout/stderr files and records the exact
   Longhouse session/thread/turn/run/request binding.
4. The child runs in its own process group with stdin closed and bypass
   permission policy only.
5. Set `LONGHOUSE_MANAGED_SESSION_ID` so the existing Claude lifecycle hook
   binds the native transcript to the Console session. Clear inherited
   channel-only and remote-approval variables; Console cannot join a Helm
   channel accidentally.
6. Persist every stream line as raw evidence before projecting user text,
   assistant text/reasoning, tools, provider identity, phase, usage, and
   terminal result.
7. Require `system.init.session_id` to match the selected provider UUID before
   promoting the binding.
8. Keep the hook-seeded native Claude JSONL as the canonical durable source.
   Do not add a second binding authority.
9. Settle completed only after a successful provider `result` record and output
   drain. Exit zero without durable terminal evidence is failed or ambiguous.
10. After Machine Agent restart, reconcile PID plus process-start identity,
    continue from the recorded byte offset, and never replay the prompt.

Start with complete structured messages. Add partial-message streaming only if
a focused projection test proves stable identities and no duplicate prose.

## Files and Seams

- `engine/src/claude_print.rs`: adapter, stream projector, process lifecycle,
  interrupt, recovery, and focused tests.
- `engine/src/main.rs`: module registration only.
- `engine/src/control_channel.rs`: Claude turn start/interrupt routing,
  recovery, dispatch support, and command tests.
- `schemas/managed_providers.yml`: promote `console_adapter`, `turn_start`, and
  `claude.turn_start` / `claude.turn_interrupt` only after proof.
- `server/zerg/config/managed_provider_contracts.json`: generated output only.
- Existing engine/server Console and provider-contract tests: extend shared
  fixtures; do not add Claude-only Runtime Host orchestration.
- Existing Claude release proof: extend to fresh plus resume through the
  production adapter.

The existing Claude hook installer and storage-v2 source remain authoritative.
If hook readiness needs a reusable helper, extract it from current channel
preflight rather than copying hook mutation into the adapter.

## Failure and Test Contract

Hermetic coverage must include:

- exact fresh/resume argv, cwd, model, and bypass policy;
- missing or invalid resume identity and stream identity mismatch;
- malformed, partial, unknown, and oversized records retained without false
  completion;
- successful result, provider-error result, exit zero without result, nonzero
  exit, and result/exit contradiction;
- duplicate turn start before spawn, after spawn, and after terminal;
- crashes after claim, after spawn, during a partial line, after result write,
  and before terminal outbox drain;
- exact PID/start identity during interrupt and recovery, including PID reuse;
- process-group cleanup and native resume after cancellation;
- missing hook/binding proof failing closed instead of creating Shadow;
- Runtime Host outage and eventual one-session convergence; and
- no tokens or secrets in argv, logs, claims, or runtime events.

The live release gate must prove fresh turn, second-process resume with recalled
context, readable prose/reasoning/tools, interrupt, post-cancel resume, Machine
Agent restart, Runtime Host outage/recovery, cold reopen, search, and no
duplicate Shadow session on the exact supported Claude version.

## Delivery Slices

1. Port the adapter onto current main with hermetic argv, stream, terminal, and
   failure tests; capability remains false.
2. Wire durable claims, interrupt, restart reconciliation, native hook/storage
   binding, and convergence tests; capability remains false.
3. Run live fresh/resume/cancel/restart/outage proof on the supported Claude
   build.
4. Update the authored manifest, regenerate contracts, and promote Claude
   Console only after all surfaces agree.

`session.launch` removal is not blocked on this plan. It may land before or
alongside Claude Console; no compatibility path in this plan depends on remote
detached launch.

## Acceptance

- Fresh and resumed Claude Console turns use one provider thread and one
  Longhouse thread.
- Exactly one invocation runs per turn and no provider process remains idle
  between turns.
- Process/result ambiguity never becomes false completion or prompt replay.
- Live and native durable records converge without a Shadow duplicate.
- Interrupt and restart recovery target exact process identity.
- Manifest claims, production dispatch, release proof, API, web, iOS, CLI, and
  local health report the same adapter-scoped capability truth.
- No Cursor remote Helm or other persistent no-terminal launch path is added.
