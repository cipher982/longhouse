# Console Turn and Transcript Convergence

Status: Proposed implementation correction 2026-07-16
Owner: Runtime Host session kernel + Machine Agent + web/iOS session clients
Related:
- `VISION.md`
- `docs/specs/turn-scoped-console-execution.md`
- `docs/specs/console-kernel-convergence.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/runtime-display-contract.md`

## Outcome

A Console session is a durable, repeatedly runnable thread. Each message owns
one bounded provider invocation. When that invocation exits, the thread becomes
ready for another message; it does not become read-only or closed.

During a turn, web and iOS show immediate, truthful progress and the complete
transcript, including tool calls. Optimistic user input converges with the
durable event exactly once.

## Incident That Exposed the Missing Seam

The 2026-07-16 iOS Codex turn had four symptoms:

1. the submitted message appeared, but there was no visible starting or working
   state;
2. the assistant transcript appeared only after the turn had completed;
3. the original user message remained as both a durable event and an optimistic
   `Sent` row;
4. the completed thread rendered as read-only.

The provider evidence is unambiguous:

- the invocation was stock `codex exec` (`originator=codex_exec`);
- it emitted a provider thread id and a normal bounded task lifecycle;
- it emitted `custom_tool_call` and `custom_tool_call_output` records for the
  actual repository search;
- `task_complete` ended the invocation after about 18 seconds.

The archived Longhouse projection contained the user and assistant prose but no
tool rows, no Longhouse input origin, and no surviving Console ownership. The
client therefore had no exact identity with which to reconcile its optimistic
row and was handed a Shadow/read-only capability projection after process exit.

The current engine code explains the identity loss directly. `read_stdout_jsonl`
wraps each decoded Codex record as:

```json
{"progress_kind": "codex_exec_jsonl", "seq": 1, "event": {"type": "thread.started", "thread_id": "..."}}
```

`CodexExecRuntimeSink.post_progress` then looks for `type` and `thread_id` on
the outer object instead of `event`. The provider-thread binding branch is
therefore unreachable for valid wrapped records. The same raw event is sent as
a generic runtime progress payload rather than a transcript event, while the
later filesystem parser drops `custom_tool_call` records. These are concrete
contract breaks, not merely slow polling.

This is not a reason to keep provider processes alive. It is an identity and
event-convergence failure between the Console turn, the provider invocation,
and the discovered transcript.

## Canonical Identities

Every Console turn carries these identities end to end:

```text
session_id          Longhouse product identity
thread_id           durable conversation and execution target
turn_id             one accepted user message through terminal outcome
run_id              one provider invocation executing the turn
client_request_id   user-submit idempotency and optimistic reconciliation key
provider_thread_id  provider-native resume identity, learned after start
```

The first five are known before provider launch. `provider_thread_id` may be
learned from the provider's first event. It is recorded as an alias on the
existing Longhouse thread; it never creates another product session.

`source_path` is useful archive evidence. It is not identity and never selects
Console versus Shadow.

## Product Invariants

1. Creating a Console session starts no provider process.
2. A normal message creates one turn; a claimed turn creates one run.
3. `task_complete`, `end_turn`, process exit, or a terminal runtime signal ends
   the run and settles the turn. It does not close the session.
4. Only an explicit user close action closes a Console session.
5. Idle Console sendability derives from its durable execution target and the
   current Machine Agent adapter proof. It does not require a live
   `SessionConnection` or provider process.
6. Provider transcript discovery can add evidence to an existing Console
   thread. It cannot demote, replace, close, or reclassify that thread.
7. A provider thread alias bound to a Console thread routes all matching
   transcript evidence to that thread.
8. User input is reconciled only by exact identity. Clients do not deduplicate
   by matching text.
9. Tool calls are raw transcript evidence and remain visible even when the
   provider invents a new tool record variant.
10. Live output may be provisional; durable archive output is canonical. The
    two lanes converge to one visible event sequence.

## One Ownership Model

```text
Console session/thread
        |
        | submit(client_request_id, message)
        v
SessionTurn queued -> starting -> active -> draining -> terminal
                         |
                         v
                    SessionRun
                         |
                         v
              Machine Agent invocation
                         |
                         +-- live provider event lane
                         |
                         +-- durable provider transcript lane
                                      |
                                      v
                         same session/thread/turn/run
```

There is no Console `SessionConnection` between turns. A temporary process or
transport record may exist for diagnostics while a run is active, but it is
not the source of composer availability.

## Machine Agent Contract

`session.turn.start` carries the full known binding:

```json
{
  "session_id": "...",
  "thread_id": "...",
  "turn_id": "...",
  "run_id": "...",
  "client_request_id": "...",
  "provider": "codex",
  "cwd": "/absolute/workspace",
  "message": "...",
  "resume_provider_thread_id": null
}
```

Before spawning, the Machine Agent durably claims `run_id` with this binding.
The claim is the local authority for attaching subsequent stdout, process, and
source-file evidence to the Longhouse turn.

When the provider reveals `provider_thread_id`, the Machine Agent:

1. persists `provider_thread_id -> session_id/thread_id/turn_id/run_id` in its
   local invocation registry;
2. emits a binding event to the Runtime Host;
3. tags live provider events with the complete binding;
4. supplies the binding to the durable source shipper when it discovers the
   matching rollout/transcript.

The durable shipper must not rely on the binding event winning a network race.
The source envelope itself carries the known Longhouse binding. The Runtime
Host validates it against the claimed run and owner before accepting it.

If a managed source envelope arrives before its Runtime Host binding is
materialized, it remains pending for that binding. It must not create a Shadow
session as a fallback.

Provider adapters decode their provider record before adding Longhouse
transport metadata. A transport wrapper must never hide fields required for
binding, event identity, phase, or terminal detection from the adapter.

## Transcript Lanes

### Live lane

The turn adapter forwards raw provider JSON events as they arrive. For Codex,
`codex exec --json` records are retained as raw evidence and minimally decoded
into provisional transcript events:

- user message;
- assistant commentary/final text;
- tool call;
- tool result;
- provider thread binding;
- turn/process state.

The live lane drives first-paint progress and transcript updates. It does not
wait for a filesystem scan.

### Durable lane

The normal provider transcript shipper remains canonical for replay, search,
export, and cold restart. It ships raw source lines with stable provider
identity plus the Longhouse binding from the invocation registry.

### Convergence

Live and durable forms reconcile with stable provider evidence, in priority
order:

1. provider event/item id;
2. provider turn id + call id for tool calls/results;
3. provider thread id + provider sequence where supplied.

Source path and byte offset remain archive cursors, not cross-lane event
identity. When durable evidence arrives, it supersedes its provisional row in
place. The UI never displays both.

Unknown provider records remain stored as raw evidence. A parser upgrade can
rebuild their transcript projection without rerunning the agent.

## Codex Tool Event Contract

The Codex parser supports both existing and current upstream shapes:

```text
function_call              -> assistant tool call
function_call_output       -> tool result
custom_tool_call           -> assistant tool call
custom_tool_call_output    -> tool result
```

For `custom_tool_call`:

- `name` is the raw tool name;
- `call_id` is the pairing identity;
- `input` is preserved verbatim and decoded as JSON only when it is valid JSON;
- the complete raw source record is retained.

For `custom_tool_call_output`:

- output text blocks are joined without discarding provider metadata;
- empty successful output still emits a completed result event;
- `call_id` pairs it with the call.

Longhouse does not rename `exec` to `Read` because the shell command happened
to read a file. The UI may summarize the raw command, but the stored tool name
remains provider truth.

## Runtime and Capability Projection

Console presentation derives from the latest nonterminal turn plus adapter
availability:

| Turn/target state | Primary status | Composer |
| --- | --- | --- |
| idle + adapter ready | Ready | enabled |
| queued | Queued | enabled for another FIFO turn if policy allows |
| starting | Starting | enabled for FIFO queueing |
| active | Working / current tool | enabled for FIFO queueing |
| draining | Finishing | enabled for FIFO queueing |
| idle + machine offline | Machine offline | disabled with typed reason |
| idle + adapter unavailable | Console unavailable | disabled with typed reason |
| explicitly closed | Closed | disabled |

The active run's terminal signal changes `turn_state` back to `idle` after
drain. It may update run history and runtime diagnostics. It cannot set the
Console session disposition to closed or change its mode to Shadow.

`session_state.mode=console` and `control.actions.start_turn` are catalog facts.
Archive convergence cannot overwrite them. `runtime_display` and compatibility
composer fields derive from those facts once; clients do not reinterpret
process death.

## Submit and Reconciliation API

Browser and iOS keep one authenticated submit veneer. The server reads the
owner-scoped catalog mode and dispatches to the explicit Console or Helm
service; clients do not select the command family from stale local state.

For Console, the response includes a turn receipt:

```json
{
  "outcome": "sent",
  "client_request_id": "ios-...",
  "turn": {
    "turn_id": "...",
    "run_id": "...",
    "state": "starting"
  }
}
```

The response is idempotent for `client_request_id`. Repeating the request
returns the same turn and run assignment.

The normalized durable user event carries:

```json
{
  "input_origin": {
    "authored_via": "longhouse",
    "client_request_id": "ios-...",
    "turn_id": "..."
  }
}
```

The Machine Agent knows the submitted prompt and binding before launch, so the
live user event can carry this origin directly. The durable reducer copies it
when the provider user event converges. No client or server text heuristic is
required.

## iOS and Web Contract

After submit, the client immediately renders the optimistic user row and the
receipt's `starting` or `queued` state. This local receipt overlay lasts only
until the workspace stream reports the same `turn_id` at an equal or later
state.

The session dock animates for `starting`, `active`, and `draining`. It does not
infer work from the presence of an optimistic row or from a generic network
request spinner.

An optimistic row is removed only when a durable/provisional user event has the
same `client_request_id` or `turn_id`. Matching text alone never removes it.

On stream reconnect, the client discards local runtime guesses and hydrates the
canonical workspace. A completed turn renders `Ready · Send`, not read-only.

## Immutable Catalog Facts

These facts are created with the Console shell and cannot be nulled or changed
by provider transcript ingest:

- session owner;
- `origin_kind=console` / `session_state.mode=console`;
- session and primary thread ids;
- provider;
- durable execution target;
- explicit closed disposition.

Archive projection may add transcript evidence, provider aliases, titles,
summaries, counters, timestamps, and run history. It may not overwrite the
facts above. Upserts use field-specific ownership rather than whole-row merge.

If provider evidence conflicts with an immutable fact, the reducer records a
diagnostic conflict and preserves the catalog fact.

## Failure Semantics

- Machine goes offline before claim: turn stays queued or returns the typed
  configured policy result; the thread remains Console.
- Command acknowledgement times out: the turn remains `starting` until the
  durable Machine Agent claim reconciles. It is not replayed automatically.
- Provider exits nonzero: run and turn fail; thread returns to idle/ready if the
  adapter remains available.
- Provider transcript is delayed: live events remain provisional and visible;
  mode and composer are unaffected.
- Binding is delayed: managed source evidence waits; it does not create a
  Shadow sibling.
- Unknown provider event shape: raw evidence remains available and runtime
  progress continues; parsing failure is observable.
- Client disconnects: turn continues; reconnect hydrates current canonical
  turn and transcript state.

## Implementation Slices

### 1. Lock the regression with captured evidence

- Add the 2026-07-16 Codex `custom_tool_call` fixture in redacted/minimal form.
- Add a create -> first turn -> terminal -> second turn contract fixture.
- Assert mode, session id, composer capability, input origin, and tool pairing
  before changing implementation.

### 2. Make binding precede transcript identity

- Add `turn_id` and `client_request_id` to `session.turn.start` and the durable
  Machine Agent claim.
- Persist provider-thread bindings in the local invocation registry.
- Attach the binding to live events and storage-v2 source envelopes.
- Validate managed bindings on ingest and defer unresolved managed envelopes.
- Prevent provider source projection from overwriting immutable Console facts.

### 3. Complete provider event projection

- Parse Codex custom tool call/result records.
- Project `codex exec --json` output into the live transcript lane.
- Reconcile live rows with durable rollout rows by stable provider evidence.
- Preserve unknown raw provider records for replay.

### 4. Project turn state end to end

- Return the Console turn receipt from the human submit veneer.
- Publish catalog turn transitions on the workspace stream.
- Derive Console status and composer availability from turn/target facts.
- Ensure terminal run events settle the turn without closing the thread.

### 5. Make clients consume the contract

- Render receipt-backed starting/queued state immediately.
- Animate canonical active/draining state.
- Reconcile optimistic rows by request/turn identity.
- Add iOS previews for Ready, Starting, Working, Finishing, Offline, and Closed.

### 6. Delete compatibility paths

- Remove Console capability promotion/demotion based on `SessionConnection`.
- Remove Console classification from provider process/session endedness.
- Remove any text-based optimistic reconciliation fallback.
- Remove any source-ingest path that can silently create a Shadow sibling for
  an explicitly bound Console invocation.

## Acceptance Gate

One real Codex Console canary must prove all of the following:

1. New Session starts zero provider processes.
2. First send produces visible `Starting` promptly and `Working` on the first
   provider/runtime event.
3. Commentary, tool call, tool result, and final answer stream before process
   exit under nominal conditions.
4. The user message appears exactly once.
5. The tool row appears and pairs with its result.
6. Process exit settles the turn and returns the same session to `Ready` with
   the composer enabled.
7. A second send launches a new run and resumes the same provider thread.
8. Session id, thread id, mode, owner, and execution target are identical
   before and after durable archive convergence.
9. Delaying the binding event or durable transcript cannot create a Shadow
   sibling or alter capabilities.
10. Duplicate submit and duplicate Machine Agent command delivery each execute
    the prompt at most once.

Automated coverage includes backend contract tests, engine parser/binding
tests, iOS unit and preview rendering, web tests, and a remote real-provider
canary. Full CI is not a substitute for the real provider canary because the
critical evidence includes upstream event shapes and resume behavior.

## Non-Goals

- Keeping Console provider processes warm between turns.
- Reconstructing provider-native history from normalized Longhouse events.
- Making Console steerable mid-turn.
- Adding a second transcript store or client-side lifecycle state machine.
- Inferring semantic file operations from arbitrary shell commands.
- Changing Helm's terminal-owned process model.
