# Provider-Neutral Directed Input v1

Status: In progress
Owner: session kernel / managed providers
Created: 2026-07-23

## Goal

Longhouse gives every managed coding-agent session the same small coordination
surface across providers:

1. discover relevant sessions;
2. inspect their raw recent work;
3. send durable attributed input;
4. recover pending or previously received input; and
5. reply without copying session identifiers.

The Runtime Host owns durable routing intent. Provider adapters own the
mechanics of placing an input into Claude, Codex, OpenCode, Antigravity, Cursor,
or a future provider. The target transcript remains the evidence of what the
model actually perceived.

This is a prelaunch replacement of the first coordination loop. There are no
legacy contracts, compatibility aliases, dual-write cutovers, or migration
shims to preserve.

## Product Contract

The user outcome is one cross-provider journey:

```text
discover -> inspect -> send -> persist -> inject -> observe -> reply
```

Longhouse guarantees only mechanical facts:

- the input was persisted;
- a delivery attempt was or was not made;
- the provider accepted the input, when observable;
- a provider turn or transcript event contained it, when observable; and
- another directed input replied to it.

Longhouse does not claim that a model saw, understood, handled, or completed an
input when the available provider evidence cannot prove that claim.

## First Principles

- Address durable sessions, never provider processes or invocations.
- A session UUID is an address, not authority.
- Persist before attempting delivery.
- Durable asynchronous input is the base behavior; live injection is an
  optimization.
- Deliver only at a provider boundary proved safe for that adapter.
- Do not auto-start or resume a cold session without a separate explicit grant.
- Preserve raw presence, capability, attempt, and transcript facts. Agents
  decide relevance and meaning.
- Peer input is attributed untrusted input below user, developer, repository,
  and safety instructions.
- MCP, CLI, HTTP, hooks, and native integrations are clients of one machine
  contract.
- Use at-least-once transport with idempotent persistence and best-effort
  injection deduplication. Do not promise exactly-once effects.

## V1 Decisions

### One envelope, one delivery kernel

The durable peer envelope owns routing intent:

```text
DirectedInput
  id
  owner_id
  source_session_id
  target_session_id
  body
  reply_to_id
  client_request_id
  created_at
```

The existing managed `SessionInput` / live-input receipt path remains the only
provider-delivery kernel. A peer envelope that is eligible for delivery creates
or references one provider input receipt whose idempotency key is derived from
the directed-input id. That receipt records the exact attributed text submitted
to the provider and the raw delivery facts.

The peer envelope does not maintain a second `queued / delivering / delivered /
stored_only / failed` state machine. Inbox and sent results join the envelope to
its input receipt and expose the receipt facts. An absent receipt means no live
delivery was attempted.

The two records have different ownership:

- directed input: what one session asked Longhouse to route;
- provider input receipt: what Longhouse mechanically attempted to place into
  the target provider;
- provider transcript event/turn: what the target model actually received.

They are linked by directed-input id, input-receipt id, and provider turn or
transcript correlation when the provider exposes it.

### Five agent primitives

```text
peers(filters?)
tail(session_id, cursor?)
send(session_id, text, client_request_id?)
inbox(after_cursor?)
reply(input_id, text, client_request_id?)
```

- `peers` returns raw repo, worktree, branch, device, provider, activity,
  presence, and current capability facts. It does not rank or select peers.
- `tail` returns bounded raw recent transcript events.
- `send` derives the sender from authenticated current-session authority.
- `inbox` is a complete durable recovery path, including inputs whose live
  delivery was unavailable or whose provider context was later compacted.
- `reply` resolves the target from the parent input and sets `reply_to_id`.

The machine API is canonical. CLI and MCP bindings stay thin.

### Delivery policy

V1 uses one conservative policy across providers:

| Target evidence | Action |
| --- | --- |
| quiescent invocation with proved input support | create receipt and inject |
| active turn | create or retain a queued receipt; do not steer |
| no current invocation | persist envelope; do not start or resume |
| observe-only / no control path | persist envelope; no live attempt |
| adapter or machine unavailable | persist envelope; expose typed attempt failure only if an attempt began |

Native busy-turn queueing, automatic resume, active-turn steer, and offline
sender outboxes are outside V1.

### Receipt semantics

Expose timestamps and identifiers rather than one `delivered` claim:

- envelope `created_at` proves persistence;
- input receipt creation proves Longhouse offered or queued provider input;
- provider acceptance timestamp proves only provider acceptance;
- provider turn/transcript correlation proves observable model input;
- `reply_to_id` proves a correlated reply exists.

Human-facing labels may be derived from those facts but are never stored as
independent authority.

### Identity and authority

The sender never appears in a model-controlled tool argument. Runtime Host
authorization must bind owner and current session before accepting `send`,
`inbox`, or `reply`.

Before implementation locks the mechanism, live experiments must prove the
simplest correct authority path for each provider:

1. strip or corrupt ambient `LONGHOUSE_*SESSION_ID` values;
2. start the provider's registered coordination adapter;
3. prove the correct managed session still resolves;
4. prove a nested or unmanaged provider cannot inherit the parent identity; and
5. prove resume creates or reacquires the intended binding.

The preferred mechanism is a short-lived session-scoped credential issued for
the managed invocation and delivered only to its coordination adapter. If a
provider cannot carry launch-scoped adapter credentials, the local Machine
Agent must validate exact provider process evidence such as PID plus process
start time before exchanging local launch evidence for that credential.
Environment session IDs alone are never authority.

### Idempotency and ordering

- The client supplies `client_request_id`; adapters generate one when omitted.
- `(owner, source_session, client_request_id)` identifies one immutable input.
- Reuse with different target or text is a conflict.
- A target inbox has stable creation order with an id cursor.
- The provider input receipt uses a deterministic request id derived from the
  directed-input id.
- A crash after provider acceptance but before correlation may cause a marked
  retry ambiguity. The system reports the raw evidence instead of claiming
  exactly-once delivery.

## Explicit V1 Cut List

Delete or do not build:

- acknowledgement, handled, seen, unread, and read-receipt state;
- `ack_message` and unacknowledged-only filtering;
- an independent peer-message delivery state machine or attempt table;
- `check_wall` and `get_session_events` as coordination tools;
- task types, priorities, routing intelligence, supervisor agents, personas,
  groups, broadcasts, and workflow state;
- generated peer summaries or LLM relevance ranking;
- attachment/content-block support for peer inputs;
- automatic cold-session launch or resume;
- active-turn delivery or steer;
- offline sender outbox synchronization;
- a workspace blackboard;
- federation or cross-owner identity;
- provider capability emulation;
- event-sourcing, schema-registry, replay, or snapshot infrastructure; and
- legacy API/tool aliases, compatibility shims, or dual storage paths.

## Implementation Shape

The current code has two peer-message writers and two delivery paths. The
catalog path already bridges a durable peer message into the real SessionInput
delivery kernel. V1 simplifies around that existing seam:

1. make the bounded catalog the only directed-input store;
2. reduce its peer-message row to envelope fields plus `reply_to_id` and real
   client idempotency;
3. link it to the existing live input receipt instead of copying delivery
   status onto the envelope;
4. delete archive/legacy `SessionMessage` delivery and runtime wake paths;
5. delete acknowledgement storage, routes, RPCs, tools, counts, tests, and
   provider instructions;
6. expose the five primitives through the canonical agents API;
7. make CLI and the installed native MCP adapter thin clients of that API;
8. remove duplicate in-process coordination tool implementations when no
   production caller remains; and
9. keep wall/session-event APIs available outside the five-tool coordination
   surface where they serve timeline or diagnostic clients.

## Execution Plan

### Phase 1: prove boundaries

- Trace every current directed-message and session-input writer/reader.
- Run managed-provider identity experiments with ambient session identity
  removed, corrupted, nested, and resumed.
- Prove idle, active, cold, and observe-only delivery behavior on the provider
  adapters needed for the first cross-provider journey.
- Establish which provider acceptance and transcript/turn correlation facts
  are genuinely observable.

Gate: identity and safe-boundary behavior are demonstrated with real provider
processes, not inferred from mocks.

### Phase 2: simplify persistence and API

- Replace the current message schema with the minimal directed-input envelope.
- Add client idempotency and reply linkage.
- Join envelope reads to existing input receipt facts.
- Delete ack and the parallel legacy message kernel.
- Implement canonical `send`, `inbox`, and `reply` routes.

Gate: focused catalog/API tests prove owner scope, self-send rejection,
idempotent replay, conflict on changed payload, stable inbox ordering, reply
target resolution, and absence of acknowledgement state.

### Phase 3: one delivery seam

- Render one fixed attributed peer-input envelope.
- Route all eligible peer input through the managed SessionInput/live-receipt
  path.
- Queue while active, inject while quiescent, and remain durable without
  starting cold or observe-only sessions.
- Persist raw attempt errors and available provider correlation.

Gate: tests prove one receipt per directed input, no direct peer-specific send
path, no implicit steer, no cold resume, and recoverable failure.

### Phase 4: thin agent surfaces

- Publish only `peers`, `tail`, `send`, `inbox`, and `reply` to managed agents.
- Remove ack, duplicate wall/event tools, dead bootstrap text, and redundant
  tool implementations.
- Keep stable coordination awareness in the provider-native mechanism that is
  actually durable for each provider.

Gate: spawn each actually registered adapter command and complete a real MCP
handshake with the exact five tools and trustworthy current-session identity.

### Phase 5: provider and live verification

- Run focused unit, catalog, engine, and core E2E targets.
- Dogfood at least two different managed providers.
- Complete a cross-provider, cross-machine round trip through the hosted
  Runtime Host.
- Prove active target queues, cold target does not resume, observe-only target
  remains durable without a live claim, spoofed identity fails, idempotent
  retry creates one envelope, and reply correlation is correct.
- Run Hatch Cursor Grok architecture reviews during the implementation and
  delete complexity that is not earning its keep. Use Hatch Fable for a
  genuinely difficult decision or final independent review if needed.

Gate: the live journey and negative cases leave durable inspectable evidence.

### Phase 6: ship

- Commit atomic slices on shared `main` while preserving unrelated work.
- Run push readiness and the exact required local test tiers.
- Push and run `make ship SHA=<task-sha>`.
- Refresh the locally installed CLI/engine and restart Longhouse.app.
- Verify the exact SHA on demo and hosted canary, consume the ship completion
  signal, and verify the live directed-input journey against the shipped code.

## Definition of Done

- One minimal directed-input envelope store exists.
- One managed SessionInput/live-receipt delivery kernel exists.
- Five agent primitives exist with CLI/API/MCP parity.
- Sender identity is session-bound and survives negative inheritance tests.
- No acknowledgement or peer-specific parallel delivery lifecycle remains.
- No cold session is started implicitly.
- Two different providers on different machines complete the live round trip.
- Busy, cold, observe-only, identity-spoof, retry, and failure cases are proven.
- Grok findings are dispositioned; final architecture remains appropriate for
  one developer and zero users.
- The exact task SHA is shipped, locally dogfooded, and live-verified.

## Review Record

- Greenfield review: Hatch Claude Fable, run
  `hatch_20260724T014845.575339000Z_e35b47bfdee8d76d`.
- Greenfield review: Hatch OpenRouter Kimi K3, run
  `hatch_20260724T014847.306986000Z_98d6e58dddd70c4b`.
- Initial repository simplification audit: Hatch Cursor Grok, run
  `hatch_20260724T022537.789461000Z_492c38a7847f865d`.
