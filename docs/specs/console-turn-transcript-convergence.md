# Console Turn and Transcript Convergence

Status: Proposed implementation correction 2026-07-16
Owner: Runtime Host session kernel + Machine Agent + web/iOS session clients
Related:
- `VISION.md`
- `docs/specs/turn-scoped-console-execution.md`
- `docs/specs/console-kernel-convergence.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/runtime-display-contract.md`
- `docs/specs/immutable-source-outbox.md`
- `docs/specs/cursor-storage-v2-source-fidelity.md`

## Outcome

A Console session is a durable, repeatedly runnable thread. Each message owns
one bounded provider invocation. When that invocation exits, the thread becomes
ready for another message; it does not become read-only or closed.

During a turn, web and iOS show immediate, truthful progress and the complete
transcript, including tool calls. Optimistic user input converges with the
durable event exactly once.

## Concrete Contract Breaks

The 2026-07-16 iOS Codex incident reduced to two code defects. First,
`read_stdout_jsonl`
wraps each decoded Codex record as:

```json
{"progress_kind": "codex_exec_jsonl", "seq": 1, "event": {"type": "thread.started", "thread_id": "..."}}
```

`CodexExecRuntimeSink.post_progress` then looks for `type` and `thread_id` on
the outer object instead of `event`. The provider-thread binding branch is
therefore unreachable for valid wrapped records. The same raw event is sent as
a generic runtime progress payload rather than a transcript event, while the
later filesystem parser drops `custom_tool_call` records. These are concrete
contract breaks, not merely slow polling. Together they produced a transcript
with prose but no tools or Longhouse input origin, followed by a Shadow/read-only
projection after the bounded invocation exited.

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

## Convergence Invariants

`turn-scoped-console-execution.md` owns process and turn lifecycle;
`console-kernel-convergence.md` owns mode, actions, and workspace projection.
This correction uniquely adds these invariants:

1. Provider transcript discovery can add evidence to a bound Console thread.
   It cannot create a second product session or change that thread's mode,
   disposition, owner, or execution target.
2. A managed invocation's source evidence always carries its Longhouse
   session/thread/turn/run binding. Binding is not a racing side-channel.
3. Live output is provisional and the durable provider transcript is
   canonical; both lanes converge to one visible event sequence in either
   arrival order.
4. User input reconciliation uses run/request/turn identity. Text comparison is
   never an identity or dedupe key.
5. Tool calls and unknown provider records remain raw source evidence even
   when the current transcript projector cannot decode them.
6. Provider run completion settles the turn. Provider ingest cannot write the
   Console session's explicit closed disposition in either direction.

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
materialized, it enters a durable pending-binding queue that survives Runtime
Host restart. Pending count, oldest age, run id, provider id, and last error are
visible in ingest health and repair diagnostics. A bounded retry window may
move the envelope to a diagnostic terminal state, but never turns it into an
unmanaged session. Operators can replay it after repairing the binding.

Provider adapters decode their provider record before adding Longhouse
transport metadata. A transport wrapper must never hide fields required for
binding, event identity, phase, or terminal detection from the adapter.

## Transcript Lanes

### Live lane

The turn adapter forwards raw provider JSON events as they arrive. For Codex,
a bounded `codex app-server --listen stdio://` process runs one turn, drains,
and exits. Its JSON-RPC notifications are retained as raw live evidence and
minimally decoded into provisional transcript events:

- user message;
- assistant commentary/final text;
- tool call;
- tool result;
- provider thread binding;
- turn/process state.

The later durable rollout JSONL remains canonical. `thread/read` is a resume
and diagnostic surface, not transcript identity: its item ids are not stable
with either app-server live ids or rollout call ids, and it can omit tool
items.

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
3. provider thread id + provider sequence where supplied;
4. within one exactly bound run, `(run_id, role, ordinal_within_run)` when
   neither lane provides a shared provider event id.

Codex app-server `item.id` and rollout `call_id` are different namespaces.
They must not be compared directly. Until an explicit adapter mapping exists,
Codex tool convergence uses the fourth, run-scoped ordinal key and preserves
both raw identities as evidence.

The fourth key is positional identity within a run that executes exactly one
turn; it is not content matching. The reducer records the ordinal for both
lanes. Whichever form arrives second resolves to the existing key: durable
evidence supersedes provisional evidence, while a late provisional event is
dropped when durable evidence already owns the key.

The first user-role record of the bound provider turn is the accepted turn
input. If a provider emits multiple user-role records in one turn, their
ordinal under the run disambiguates them. The accepted input's
`client_request_id` and `turn_id` are copied from the run binding, never
inferred from text.

Source path and byte offset remain archive cursors, not cross-lane event
identity. When durable evidence arrives, it supersedes its provisional row in
place. The UI never displays both.

`immutable-source-outbox.md` and `cursor-storage-v2-source-fidelity.md` own raw
durable source retention. A skipped/unknown projector record still retains its
raw source line. For the live lane, the run-scoped raw provider event is stored
as a runtime observation until durable evidence supersedes it. A parser upgrade
can rebuild the durable transcript projection without rerunning the agent.

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

The Rust `CodexPayload` model gains the upstream `input` field; adding match
arms without extending that raw payload model is incomplete.

For `custom_tool_call_output`:

- output text blocks are joined without discarding provider metadata;
- empty successful output still emits a completed result event;
- `call_id` pairs it with the call.

Longhouse does not rename `exec` to `Read` because the shell command happened
to read a file. The UI may summarize the raw command, but the stored tool name
remains provider truth.

## Runtime and Capability Boundary

The existing kernel specs own the Console state/composer truth table. This
correction requires only that `starting`, `active`, and `draining` transitions
from the bound run reach that projection, and that a terminal run returns the
open thread to its canonical idle state. Transcript ingest cannot promote or
demote kernel actions and cannot close or reclassify the session.

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
    "run_id": null,
    "state": "queued"
  }
}
```

The response is idempotent for `client_request_id`. Repeating the request
returns the same turn identity plus its current state and current nullable run
assignment. A queued turn has no `run_id` until dispatch claims it; an
idempotent replay may therefore show a later state and newly assigned run.

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
onto the first user-role record of that exactly bound run. No client or server
text heuristic is required.

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

Provider transcript ingest cannot null or change these catalog-owned facts:

- session owner;
- `origin_kind=console` / `session_state.mode=console`;
- session and primary thread ids;
- thread-owned provider and durable execution target.

Explicit closed disposition is owned only by the user close action. Provider
ingest may neither close nor reopen the session.

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
- Change the Codex sink boundary to accept the decoded provider event, derive
  binding/phase/transcript facts, and only then wrap it with Longhouse transport
  metadata. A one-line nested-field lookup inside the current wrapper is not
  the intended fix.
- Persist provider-thread bindings in the local invocation registry.
- Attach the binding to live events and storage-v2 source envelopes.
- Validate managed bindings on ingest and route unresolved managed envelopes
  through durable `pending_binding -> blocked_binding` diagnostics.
- Prevent provider source projection from overwriting immutable Console facts.

### 3. Complete provider event projection

- Parse Codex custom tool call/result records.
- Project bounded Codex app-server notifications into the live transcript lane.
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
- Add `turn_id` to submitted-input identity and workspace stream projections;
  reconcile optimistic rows by request/turn identity.
- Add iOS previews for Ready, Starting, Working, Finishing, Offline, and Closed.

### 6. Delete compatibility paths

- Remove Console capability promotion/demotion based on `SessionConnection`.
- Remove Console classification from provider process/session endedness.
- Remove any text-based optimistic reconciliation fallback.
- Enforce the convergence invariant that bound source ingest cannot create a
  second product session.

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
9. Delaying the binding event or durable transcript exercises the durable
   pending/blocked binding diagnostic without creating a second session or
   altering capabilities.
10. Duplicate submit and duplicate Machine Agent command delivery each execute
    the prompt at most once.
11. Durable-before-live and live-before-durable arrival orders produce the
    same ordered event identities, including user and assistant prose.
12. An unknown provider record retains its raw durable source line even when it
    has no current transcript projection.

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
