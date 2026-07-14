# Turn-Scoped Console Execution

Status: Proposed canon; supersedes the Console execution-lifetime decisions in
`machine-directory-and-console-launch.md`
Owner: session kernel + Machine Agent + web/iOS session clients
Created: 2026-07-14
Related:
- `VISION.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/machine-directory-and-console-launch.md`
- `docs/specs/agents-machine-surface.md`

## Decision

Longhouse persists **threads**, not idle provider processes.

For Console, one user message starts one provider turn. The provider invocation
exists while that turn is nonterminal and is reaped after the provider reports
the terminal turn outcome. A later message resumes the durable provider thread
in a new invocation.

Process persistence is not a session mode, a user preference, or a requirement
for continued conversation. It may exist while a user has an interactive Helm
TUI open, or as a bounded implementation cache in the future, but correctness
must never depend on it.

The current `one_shot` name describes the desired process lifetime but obscures
the product semantics: the **turn** is one-shot, not the conversation. The
current `live_control` Console option incorrectly makes provider-process
lifetime part of the user model. Both are replaced by one Console behavior:
turn-scoped execution.

## Why

The durable object the user returns to is a conversation thread. An idle
Claude, Codex, Cursor, or OpenCode process adds no product value when Longhouse
can resume the same provider thread for the next message. Keeping one process
per Console conversation would make idle resource use grow with historical
conversation count and require cleanup policy for a resource that should not
exist.

Longhouse already has most of the correct kernel:

- `SessionThread` is causal continuity that survives quit/resume;
- `SessionTurn` is one user submission through terminal provider outcome;
- `SessionRun` is one provider CLI process invocation;
- `SessionConnection` is a control attachment to that invocation.

The remaining error is in launch and capability semantics. Today an idle user
can only send if a live `SessionConnection` exists, and Console creation asks
the user to choose `one_shot` or `live_control`. This conflates the durable
thread, active turn, invocation, and control window.

## Canonical Nouns

**Session** — the product object in the timeline. It owns title, workspace,
archive state, and a primary thread.

**Thread** — durable conversational continuity. It owns Longhouse identity and
provider resume evidence. It may be idle with no process.

**Turn** — one user submission and all provider work through a settled terminal
outcome. Tool calls, tool results, permission pauses, and model calls remain
inside the same turn. A provider signal such as `end_turn` begins the final
drain; the turn is settled only after output capture and invocation exit.

**Invocation** — one provider process used to execute a turn. In the current
schema this is `SessionRun`. A new turn normally creates a new invocation.

**Connection** — Longhouse's temporary ability to observe or control an active
invocation. It does not make a thread durable or resumable.

**TUI lease** — a Helm-only process lifetime owned by the user's open terminal.
It may span several turns because the TUI itself is the selected user
interface. This is not a Console execution option and does not change the
thread or turn model.

## Invariants

1. A thread may exist indefinitely with no provider process.
2. An idle Console thread has zero provider invocations.
3. A Console thread has at most one active normal turn and one execution owner.
4. A provider terminal signal moves the turn into `terminal_draining`. The
   execution owner remains held until stdout, stderr, and transcript capture is
   finalized and the invocation exits or is forcibly reaped. Only then is the
   turn settled and the next queued turn may start.
5. Starting a later turn resumes provider-owned thread state; it does not
   reconstruct the provider solely from Longhouse-rendered messages.
6. A connection grants actions against an active invocation. It is not the gate
   for starting a new turn on an idle thread.
7. Provider process death fails or interrupts a turn, not the thread.
8. A provider may be implemented with a short-lived helper server, but that
   server is disposable infrastructure and must not become per-thread durable
   truth.

## Durable State Is More Than a Message List

The user's intuition is directionally right: conversation continuity belongs
in durable state, not an idle process. Longhouse must not assume that a
normalized list of messages is sufficient to reproduce that state, however.

Provider-owned thread state can include:

- provider session/thread identity;
- compaction and hidden context;
- tool and approval history;
- model, mode, and reasoning settings;
- provider-specific metadata and workspace bindings.

Longhouse keeps its raw durable archive and the provider resume identifiers.
Each provider adapter resumes the provider's native thread. Reconstructing a
provider thread from normalized events is a future explicit recovery operation,
not the normal send path.

## Turn Boundary and Tool Waits

A turn is active until the provider emits its real terminal signal. A long
Hatch invocation illustrates the rule:

1. the model emits a Hatch tool call;
2. the Hatch process runs for seconds or minutes;
3. the tool result returns;
4. the model is called again with that result;
5. the model emits its final response and `end_turn`.

The provider invocation remains alive through steps 1–5 because the turn never
ended. This is not idle process persistence.

If a tool deliberately starts independent background work and immediately
returns a durable handle, the turn may end. That external work is then a job or
machine process, not an active agent turn. Longhouse must not keep the provider
invocation alive merely because such work exists. A later turn can inspect or
wait on the handle. The handle is transcript evidence only: Longhouse does not
imply ownership, liveness, cancellation, or automatic wake-up for it.

Runtime phases such as `thinking`, `running_tool`, `blocked`, and `waiting` are
states inside a nonterminal turn. `idle` is a thread state after a terminal
turn, not proof that an invocation should remain alive.

## Messages While a Turn Is Active

Submitting text has three distinct meanings:

| Thread state | Meaning | Behavior |
| --- | --- | --- |
| no active turn | start next turn | spawn/resume one invocation and send the message |
| active turn, provider supports steering | steer current turn | deliver to the active invocation without creating a second turn |
| active turn, provider cannot steer | queue next turn | accept durably and start it after the active turn terminates |

Answers to provider permission or structured-question pauses are responses
inside the active turn, not new turns.

Longhouse does not start two normal turns concurrently on the same thread. A
future explicit fork can create a child thread when parallel work is desired.

A queued message creates a durable `SessionTurn(state=queued, run_id=NULL)` and
input record in one transaction. FIFO dispatch atomically claims the oldest
queued turn, creates and binds its run, and changes it to `dispatching`; only
then is `session.turn.start` sent. The target states are `queued`,
`dispatching`, `active`, `terminal_draining`, `completed`, `failed`, and
`cancelled`. `dispatching`, `active`, and `terminal_draining` hold the thread's
single execution-owner lease.

## Console UX

**New Session** creates an empty, ready thread from:

- machine;
- provider;
- workspace;
- advanced provider settings, if any.

It does not ask for a Task, first prompt, run lifetime, or **Keep runtime open**.
Creation opens the normal empty conversation. The composer is the only place
the user sends the first and later messages.

The empty thread exists in Longhouse before a provider-native thread id exists.
The first turn creates that provider thread and records its resume identity.
The thread therefore owns its durable execution target: `device_id`, `cwd`,
provider, and non-secret provider settings. `SessionRun` copies the effective
target as historical evidence; it cannot route the first turn because no run
exists yet.

The composer is enabled by `can_start_turn`, not by
`live_control_available`. During an active turn it becomes a steer or queued
next-message affordance according to the provider capability.

## Capability Contract

The current capability model overloads `can_send_input`. Split it into:

- `can_start_turn`: an idle thread can acquire an invocation on its recorded
  machine and provider;
- `can_steer_active_turn`: text can be injected into the current turn;
- `can_queue_next_turn`: text can be accepted durably while a turn is active;
- `can_interrupt_active_turn`;
- `can_answer_pause`;
- `can_terminate_invocation`;
- `can_resume_thread`: the provider resume identity is sufficient for a later
  invocation.

`live_control_available` continues to describe an active connection. It must
not answer whether an idle Console conversation can receive another message.

The projection has two independent axes: thread execution and active-invocation
control. `control_label` describes only the active invocation. A Console thread
whose latest run closed successfully remains an idle managed thread, not
`imported/process_ended`.

The target flat response is:

```text
{
  turn_state,                    # idle | queued | dispatching | active | terminal_draining
  can_start_turn,
  can_queue_next_turn,
  can_resume_thread,
  start_turn_blocked_by,
  live_control_available,
  can_steer_active_turn,
  can_interrupt_active_turn,
  can_answer_pause,
  can_terminate_invocation,
  can_tail_output,
}
```

`can_start_turn` is derived from durable thread placement plus current machine
availability and provider adapter support. It is false when the machine is
offline, the workspace is invalid, or the provider cannot start/resume a
turn. The thread remains visible and durable in all three cases.

Active-turn capabilities come from the selected invocation adapter and its
current connection, not the provider's aggregate contract. `codex_exec` is not
steerable merely because `codex_bridge` is; `cursor_acp` is not steerable.
Unsupported adapters queue the next turn instead.

## Provider Evidence

All four intended Console providers expose a process-per-turn primitive in the
currently installed CLIs:

| Provider | Fresh turn | Resume turn | Current Longhouse state |
| --- | --- | --- | --- |
| Codex | `codex exec` | `codex exec resume <thread>` | implemented as `codex.run_once` / `resume_run_once` |
| Cursor | ACP `session/new` + `session/prompt` | ACP `session/load` + `session/prompt` | implemented as `cursor.run_once` / `resume_run_once` |
| Claude | `claude --print --session-id <id>` | `claude --print --resume <id>` | CLI proof exists; managed turn adapter missing |
| OpenCode | `opencode run` | `opencode run --session <id>` | CLI proof exists; managed turn adapter missing |

Cursor's ACP response already provides the exact terminal boundary through
`stopReason: end_turn`. Codex exits after the exec turn. Claude stream JSON has
a result event and then exits. OpenCode JSON run output terminates when the run
completes.

Therefore Claude and OpenCode are not blocked by a need for persistent
processes. They need the same managed turn adapter, runtime-event capture,
resume binding, interruption, and proof already built for Codex/Cursor.

## API Shape

The canonical machine surface should express thread and turn operations rather
than provider-process lifetime:

```text
POST /api/agents/sessions
  create an empty Longhouse session + primary thread

POST /api/agents/sessions/{session_id}/turns
  durably accept a user message and start or queue the next turn

POST /api/agents/sessions/{session_id}/turns/{turn_id}/steer
POST /api/agents/sessions/{session_id}/turns/{turn_id}/interrupt
```

Browser and iOS routes are user-auth veneers over the same service. Existing
`/api/sessions/launch`, `/input`, `session.launch`, and `session.run_once`
contracts may remain during migration, but clients must not keep choosing an
execution lifetime.

The Machine Agent converges on one semantic command:

```text
session.turn.start {
  session_id,
  thread_id,
  turn_id,
  client_request_id,
  run_id,
  provider,
  cwd,
  message,
  provider_resume_identity?
}
```

The provider adapter decides fresh-versus-resume from the resume identity. In
one Runtime Host transaction, create or return the `SessionTurn` keyed by
`(thread_id, client_request_id)`, record the accepted message, create exactly
one `SessionRun`, and set `SessionTurn.run_id` before dispatch. A terminal event
applies only to the turn owning that `run_id`. Retrying a client request returns
the same turn/run and never sends a second provider prompt.

Runtime Host idempotency is not sufficient because command delivery can be
ambiguous across a Machine Agent crash. The command id is deterministically
derived from `run_id`. Before spawning, the Machine Agent durably claims that
run id in its local invocation registry. Duplicate commands return the existing
claim/result and never spawn again.

The claim records `claimed`, `spawned`, and `terminal` plus PID,
boot/process-start identity, provider identity when known, and result metadata.
Recovery reconciles the claim against the owned process group and transcript
evidence. If a crash occurred between claim and provable spawn state, recovery
fails closed instead of sending the prompt again. A new user retry is allowed
only after reconciliation proves the prior invocation did not deliver it.

## Invocation Cleanup

For Console, terminal turn handling must:

1. record the provider terminal signal and enter `terminal_draining`;
2. finish output and transcript capture;
3. terminate any provider helper process owned only by that invocation;
4. close the `SessionRun` with exit status;
5. release/end its `SessionConnection`;
6. finish or fail the `SessionTurn`;
7. preserve provider resume identity and transcript evidence;
8. release the execution owner and dispatch the next queued turn, if any.

Cleanup is event-driven from the provider terminal signal and process exit,
with a bounded orphan reaper as crash recovery. The reaper validates PID plus
boot/process-start identity and terminates the owned invocation process group,
not merely the direct CLI child. A periodic idle-session reaper must not be the
primary lifecycle mechanism.

## Migration Plan

### Slice 1 — Ship the first vertical turn

- Adopt this spec and update `VISION.md`.
- Add `can_start_turn` and the active-turn capabilities to the server-owned
  projection.
- Create an empty thread and navigate directly to its normal conversation.
- Make the composer start/resume a turn when no turn is active.
- Do not ship empty-thread creation before this composer dispatch path.

### Slice 2 — Remove process lifetime from the launch UI

- Remove **Task**, first-message, `execution_lifetime`, and **Keep runtime open**
  from web/iOS launch UX after Slice 1 works end to end.
- Serialize normal turns per thread; durably queue unsupported mid-turn sends.
- Stop gating the idle composer on `SessionConnection.can_send_input`.
- Keep current backend launch fields only as compatibility inputs.

### Slice 3 — Unify machine dispatch

- Add `session.turn.start` and route Codex/Cursor through it.
- Treat `SessionTurn` as the idempotent dispatch record and `SessionRun` as the
  invocation record.
- Remove new call sites of `session.launch` and `session.run_once`; retain
  compatibility translation until all installed Machine Agents are upgraded.

### Slice 4 — Provider parity

- Implement Claude print/resume turn execution with channel/hook transcript
  binding, pause answers, steering where supported, and terminal proof.
- Implement OpenCode run/session execution with JSON event capture, abort, and
  terminal proof.
- Promote `run_once` evidence for both providers only after real resume-same-
  thread tests pass.

### Slice 5 — Delete process-lifetime product state

- Remove Console `live_control` versus `one_shot` policy and UI copy.
- Rename remaining internal `one_shot` concepts to `turn_scoped` or delete
  them where the turn record makes the distinction redundant.
- Delete per-thread idle bridges and their user-facing reattach semantics for
  Console.
- Keep Helm TUI attachment as an explicit terminal-owned transport.

## Acceptance Criteria

- Creating a Console session starts no provider process.
- Sending the first message starts one process and produces one turn.
- After `end_turn`, output drains, the process is reaped, and only then does the
  composer become ready for the next turn.
- Sending a second message starts a new process and resumes the same provider
  thread without transcript or tool-state loss.
- A multi-minute tool call keeps one turn/invocation active until its result and
  final assistant response arrive.
- Mid-turn input steers or queues according to advertised capability; it never
  starts a concurrent normal turn on the same thread.
- Machine-offline state disables starting the turn without hiding or deleting
  the thread.
- Web and iOS consume the same server-owned capability and launch contract.
- No Console UI exposes process lifetime.

## Non-Goals

- Reconstructing provider-native state from normalized Longhouse messages.
- Keeping a provider warm as a launch-latency optimization.
- Parallel turns on one thread.
- Turning background OS processes into a jobs product.
- Changing the normal upstream TUI experience of Helm sessions.
