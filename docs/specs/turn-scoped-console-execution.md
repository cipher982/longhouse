# Turn-Scoped Console Execution

Status: Active canon; implementation plan locked 2026-07-14
Owner: session kernel + Machine Agent + web/iOS session clients
Supersedes: Console `one_shot` / `live_control` execution-lifetime choices
Related:
- `VISION.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/machine-directory-and-console-launch.md`
- `docs/specs/agents-machine-surface.md`
- `docs/specs/console-turn-transcript-convergence.md`

> **Transcript convergence correction (2026-07-16):**
> `console-turn-transcript-convergence.md` defines the required binding from a
> Console turn/run to provider live events and durable transcript discovery.
> Turn-scoped execution is not complete when process lifecycle works but the
> resulting transcript can reappear as a Shadow session.

## Decision

Longhouse persists conversation threads, not idle provider processes.

Console has one execution model: a message creates a turn; one provider
invocation executes that turn; the invocation drains and exits; a later message
resumes the durable provider thread in a new invocation.

Process lifetime is not a user setting. **New Session** creates an empty thread
and opens the normal conversation. The composer sends the first and every later
message. Console exposes no Task field, first-message field, **Run once**,
**Keep runtime open**, `one_shot`, or `live_control` choice.

Helm is separate. Its process may remain alive because the user has deliberately
kept the provider's interactive TUI open. That terminal-owned lease is not a
second Console mode and is not session identity.

## The Five Nouns

**Session** — the product item in the timeline.

**Thread** — durable conversational continuity. It owns provider identity and
the next-turn execution target: provider, machine, workspace, and non-secret
provider settings. It may remain idle indefinitely with no process.

**Turn** — one accepted user message and the provider work it causes, through
the settled terminal outcome.

**Invocation** — one exclusive provider-worker lease executing one Console
turn. This is the existing `SessionRun` concept. The worker may come from a
small anonymous machine-global pool; it is never invocation identity.

**Adapter** — Machine Agent code that translates a provider-neutral turn into
one upstream provider CLI invocation.

Everything else is state or transport, not another product noun.

## Invariants

1. An idle Console thread owns no provider process. The Machine Agent may keep
   at most the configured one or two anonymous warm worker process groups,
   regardless of session count.
2. A thread has at most one execution owner.
3. A normal message always creates a turn; it never implicitly creates a new
   session or process-lifetime mode.
4. A provider `end_turn` or equivalent signal begins draining. The turn settles
   only after output capture finishes and its exclusive worker lease is
   released; the anonymous worker is then retired or safely returned to the
   bounded pool.
5. The next queued turn cannot start before the previous invocation settles.
6. Provider process death fails or cancels a turn, never the thread.
7. Later turns resume provider-native state. Longhouse does not reconstruct the
   provider from normalized messages during the normal path.
8. Unsupported adapter behavior returns a typed unavailable result. There are
   no no-op adapters and no lifecycle fallbacks.

## One State Machine

```text
queued -> starting -> active -> draining -> completed
                     |           |
                     +---------> failed
                     +---------> cancelled
```

`starting`, `active`, and `draining` hold the thread's execution-owner lease.
Terminal states release it. The scheduler then claims the oldest queued turn.

A Runtime Host transaction creates the durable input and
`SessionTurn(state=queued, run_id=NULL)`. Dispatch atomically claims that turn,
creates one `SessionRun`, stores `SessionTurn.run_id`, and moves the turn to
`starting` before contacting the Machine Agent.

No separate launch lifecycle is required for Console. `SessionTurn` owns
queue/dispatch lifecycle; `SessionRun` owns process lifecycle; the existing
`SessionLaunchAttempt` path is not used by the final Console design.

## Messages During Active Work

A normal composer send creates the next turn:

- if the thread is idle, the scheduler starts it immediately;
- if another turn owns execution, it remains queued in FIFO order.

At cutover, steering is not a Console capability. Normal send queues the next
turn. Steering may return later as an explicit active-turn action after the
first turn-scoped adapter proves it; it will never overload normal send.

Permission answers and structured-question answers belong to the current turn.
They do not create new turns, but no turn-scoped adapter proves answerable
pauses at cutover. Each adapter declares the non-blocking approval policy its
turns run under. An adapter that can stall indefinitely on an unanswerable
permission prompt is not Console-ready.

Interrupt cancels the active turn. After it drains and releases the execution
owner, the scheduler starts the oldest queued turn. A separate explicit queue
clear action may be added if users need **stop everything** semantics; interrupt
does not silently discard accepted messages.

Longhouse never runs two normal turns concurrently on one thread. Parallel work
requires an explicit child thread or fork.

## Tool Waits and Background Work

A tool wait is inside the turn. If an agent invokes a delegated tool, waits five minutes
for its result, consumes that tool result, and then responds, the invocation
remains active for the full sequence. No persistence exception is involved; the
turn simply has not ended.

If a tool deliberately returns a handle to independent background work, the
turn may end. The handle is transcript evidence only. Longhouse does not imply
ownership, liveness, cancellation, or automatic wake-up for that external
process, and it does not keep the provider invocation alive for it.

## Architecture

```text
web / iOS / CLI
       |
       v
session + turn service          durable thread, input, turn, run
       |
       v
per-thread FIFO scheduler       one execution owner
       |
       v
Machine Agent: session.turn.start(run_id, ...)
       |
       v
Console turn adapter            provider-specific CLI mechanics
       |
       v
user-installed provider CLI
```

The Runtime Host is provider-neutral. It owns durable identity, ordering,
idempotency, placement, and user-facing capability projection.

The Machine Agent owns process execution, adapter selection, raw provider
events, interruption, process-group cleanup, and local crash recovery.

The adapter boundary is intentionally small:

```text
start(context, message, optional_resume_identity) -> invocation handle/events
interrupt(handle)                   # optional graceful cancel
```

The Machine Agent always owns force termination of its invocation process
group; graceful interrupt is only an optimization. Queueing is a Runtime Host
behavior; that is not a provider capability either. `steer` or `answer_pause`
joins this boundary only when a turn-scoped adapter first proves it.

Capabilities belong to the selected adapter, not the provider brand. For
example, `codex_exec` does not become steerable merely because the separate
Codex Helm bridge can steer.

### Approval policy at cutover

Turn-scoped Console initially supports the existing `bypass` permission mode
only. Each adapter maps it to a non-interactive provider policy while preserving
the configured sandbox. `remote_approve` is unavailable until an adapter proves
pause detection and same-turn answer delivery. This is a deliberate cutover
constraint: Console must not hang on an approval prompt nobody can answer.

## Provider Evidence, Not Product Taxonomy

Provider names choose adapters; they do not define launch modes.

| Console adapter | Fresh turn | Resume turn | Current state |
| --- | --- | --- | --- |
| `codex_exec` | `codex exec` | `codex exec resume <thread>` | execution exists; promote behind common contract |
| `cursor_print` | `create-chat` + native `--print --resume` | native `--print --resume` | implementation locked; promote only after live product proof |
| `claude_print` | `claude --print --session-id` | `claude --print --resume` | feasibility evidence only; managed adapter and live resume proof needed |
| `opencode_run` | `opencode run` | `opencode run --session` | feasibility evidence only; managed adapter and live resume proof needed |

Console eligibility requires proven fresh-turn and same-thread resume behavior.
Command help is evidence of feasibility, not release proof. Each adapter must
also prove transcript binding, terminal detection, process cleanup, and the
approval policy it uses. Command help is not a run canary.

Helm adapters remain separate because they preserve the upstream interactive
TUI. They are not selected by Console and do not share Console lifecycle policy.

## Durability and At-Most-Once Execution

The Runtime Host accepts `client_request_id` on turn creation. In one
transaction it creates or returns the same input, turn, and run assignment.
Repeating a client request never creates a second turn.

The Machine Agent command id is the `run_id`. Before spawning, the Machine
Agent durably claims that run id in its local invocation registry. A duplicate
command returns the existing claim or result and never spawns again.

The claim records `claimed`, `spawned`, and `terminal`, plus PID,
boot/process-start identity, provider identity when known, and terminal result.
After a crash, the Machine Agent reconciles the claim against the exact process
group and transcript evidence. An ambiguous claimed invocation fails closed;
it is never automatically replayed. A retry with a new turn is allowed only
after reconciliation proves the prior prompt was not delivered.

This durable registry is new Machine Agent work. The current in-memory
completed-command cache and in-flight set are not substitutes. On control
channel reconnect, the Runtime Host reconciles nonterminal turns from durable
claim results rather than relying on the original live command-result frame.

This is honest at-most-once process execution. Longhouse does not claim
distributed exactly-once delivery where the provider offers no idempotency key.

## Canonical API

```text
POST /api/agents/sessions
  create empty session + primary thread + execution target

POST /api/agents/sessions/{session_id}/turns
  accept a normal message; start now or queue FIFO

POST /api/agents/sessions/{session_id}/turns/current/interrupt
```

Browser and iOS routes are user-auth veneers over the same service.

`session-identity-kernel.md` owns the full cross-mode capability projection.
The Console-relevant user-facing subset stays small:

```text
{
  turn_state,                 # idle | queued | starting | active | draining
  can_start_turn,
  start_turn_blocked_by,
  can_interrupt_active_turn,
}
```

`can_start_turn` derives from the thread's durable execution target, current
machine reachability, workspace validity, and a proven fresh/resume adapter.
Interrupt is available while the Machine Agent owns the active invocation.
Future proven active-turn controls extend this response additively.

Raw diagnostics may expose adapter, connection, resume, and process detail.
Clients do not infer product actions from raw Machine Agent support strings.

## What We Delete

After compatibility migration:

- Console `execution_lifetime` and its `one_shot` / `live_control` values;
- launch-form `initial_prompt`, Task, and **Keep runtime open**;
- provider-facing `session.launch`, `session.run_once`, and
  `session.resume_run_once` as product semantics;
- remote detached persistent Console launch paths, according to the disposition
  table below;
- Console use of `SessionLaunchAttempt`;
- provider-level lifecycle booleans that duplicate adapter capabilities;
- composer gating on `SessionConnection.can_send_input` for idle threads.
- `wrap_console_run_once_prompt` / `console_prompt.rs`; the first provider
  message is exactly the composer message, without hidden first-turn wrapping.

| Current path | Console disposition | Helm disposition |
| --- | --- | --- |
| Codex detached-UI bridge launch | delete after `codex_exec` cutover | keep Codex bridge + TUI attach |
| Claude remote channel launch | delete after `claude_print` resume proof | keep `longhouse claude` channel/PTY |
| OpenCode remote server-bridge launch | delete after `opencode_run` resume proof | keep `longhouse opencode` serve + TUI attach |
| Cursor ACP | keep as Console adapter | n/a |
| Cursor Helm PTY | n/a | keep |

Installed older Machine Agents may receive temporary command translation at one
explicit compatibility boundary. New code must not add callers to the legacy
commands.

## Implementation Plan

### 1. Build the provider-neutral path behind the current UI

- Persist the empty thread execution target.
- Add the turn transaction, FIFO scheduler, execution-owner lease, and compact
  capability projection.
- Add `session.turn.start` with durable Machine Agent run claims.
- Route existing `codex_exec` and `cursor_acp` through the adapter boundary.
- Declare and prove each adapter's non-blocking approval policy.
- Prove first turn, resume turn, queueing, drain/reap, duplicate dispatch, and
  Machine Agent crash recovery.

Do not expose empty-thread creation to users yet.

### 2. Reach provider parity

- Implement `claude_print` using stream JSON plus native session resume.
- Implement `opencode_run` using JSON output plus native session resume.
- Prove same-thread resume and transcript binding with real provider canaries.
- Advertise only adapter controls that are actually proven.

### 3. Cut over the product atomically

- Make **New Session** create the empty thread and open the conversation.
- Make the composer create the first and later turns.
- Remove Task, first-message, and process-lifetime controls from web and iOS.
- Switch the machine directory to providers with proven Console adapters.

The UI cutover occurs only after the empty-thread composer path and intended
provider set pass end-to-end tests.

### 4. Delete the old split

- Remove Console `live_control` and `one_shot` orchestration branches.
- Remove the engine `COMMAND_RUN_ONCE` handler, `console_prompt.rs`, persistent
  remote Console launch paths, and legacy capability inference.
- Remove Console launch-attempt/readiness plumbing made redundant by turns.
- Collapse provider manifest data to adapter identity, proof, and optional
  active controls.
- Keep Helm launch and control code explicit and separate.

## Acceptance Gate

- Creating 500 Console sessions never creates a process per session; the
  anonymous machine-global warm pool remains capped at one or two process
  groups.
- First send creates one turn and one invocation.
- `end_turn` drains output, releases the exclusive lease, and leaves the thread
  idle; the anonymous worker is retired or safely returned to the bounded pool.
- Second send creates a new invocation and resumes the same provider thread.
- A long tool wait keeps exactly one active invocation until the turn settles.
- Normal mid-turn sends queue FIFO; no Console steer affordance ships at
  cutover.
- Duplicate Runtime Host or Machine Agent delivery never spawns twice.
- Machine-offline state preserves the thread and explains why a turn cannot
  start.
- Web and iOS consume the same server-owned contract.
- No Console UI or public API exposes provider process lifetime.

## Non-Goals

- Reconstructing provider-native state from normalized Longhouse messages.
- Per-session warm provider processes or unbounded process caches.
- Parallel normal turns on one thread.
- A general background-jobs product.
- Changing the upstream TUI experience of Helm sessions.
