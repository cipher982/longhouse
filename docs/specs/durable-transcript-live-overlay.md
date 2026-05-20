# Durable Transcript And Live Overlay Contract

Status: Draft implementation plan

## Executive Summary

Longhouse transcript truth must come from durable source records only. Provider JSONL, parsed provider events, and explicit Longhouse-authored control inputs may become archive events. Runtime observations, bridge deltas, liveness, phase changes, and preview text are evidence about what is happening now; they must not masquerade as transcript history.

The current Codex managed bridge violates that boundary by materializing `codex_bridge_live` observations into `AgentEvent` rows with `event_origin=live_provisional`. The resulting rows are cumulative text snapshots, not transcript events. They can merge multiple assistant messages, omit tool calls, and reorder user input relative to assistant output when durable ingest lags.

The new contract is:

- `AgentEvent` is durable archive truth.
- `SessionObservation` stores runtime and transcript-source evidence.
- live transcript preview is a derived overlay from runtime observations, separate from transcript event lists.
- durable session APIs, search, counts, recall, export, and archive views must never include runtime preview rows.
- when managed control sends user input, Longhouse records a durable control input fact and later reconciles it with provider transcript echo if needed.

## First Principles

### Transcript Truth

An archive transcript event answers: "what happened in the provider or in Longhouse control history?"

Valid archive sources:

- parsed provider transcript source lines, keyed by `source_path` and `source_offset`
- raw provider event IDs and parent IDs when available
- durable Longhouse control inputs sent through web/iOS/agent control paths

Invalid archive sources:

- bridge delta buffers
- accumulated `live_text`
- phase/liveness signals
- UI previews
- current tool names without the provider transcript event

### Runtime Evidence

A runtime observation answers: "what did the machine/runtime report recently?"

Examples:

- `codex_bridge_live` text deltas and accumulated text
- bridge attached/detached state
- process liveness and managed leases
- current phase/tool information

Runtime evidence may power overlays, cards, and freshness indicators. It is not searchable archive truth.

### Causal Ordering

Durable provider events should be ordered by source order first, then timestamp as a fallback. Longhouse-authored control inputs should carry a control request identity so the UI can place a pending prompt causally before live assistant output even when provider JSONL ingest has not caught up.

## Architecture

### Durable Archive Lane

`provider JSONL -> parser -> AgentEvent`

Properties:

- one parsed provider event maps to one `AgentEvent` when it has transcript meaning
- source offsets and raw JSON are retained
- tool calls and tool results keep stable `tool_call_id` pairing
- no runtime preview payloads are inserted into this lane

### Runtime Observation Lane

`provider bridge/hook/runtime signal -> SessionObservation`

Properties:

- observations are raw evidence
- observations can be replayed into runtime read models
- bridge transcript deltas remain observations with kind `bridge_transcript_delta`
- no bridge transcript observation creates an `AgentEvent`

### Live Overlay Lane

`latest relevant SessionObservation rows -> live overlay response`

Properties:

- derived at read time for launch-scale performance
- one active preview per session/turn is enough for cards and live status
- overlay has explicit state such as `streaming`, `complete_waiting_for_archive`, or `stale`
- overlay is not counted as a transcript entry
- overlay is removed or marked superseded when durable activity catches up

The current `SessionTranscriptPreviewResponse` can remain as the transport shape during migration, but its backing data must come from observations, not `AgentEvent`.

## Decisions

### Decision: Make `AgentEvent` Durable-Only

**Context:** Mixed durable and live-provisional rows broke transcript truth.

**Choice:** Archive queries only return rows whose origin is durable or legacy-null. Bridge live snapshots do not create `AgentEvent` rows.

**Rationale:** A cumulative text buffer lacks event boundaries, tool structure, and source offsets. It cannot be made safe with predicates.

**Revisit if:** A provider exposes a stable, structured live event stream with the same IDs and boundaries as its eventual transcript.

### Decision: Derive Live Preview From `SessionObservation`

**Context:** The runtime observation table already stores `codex_bridge_live` payloads and timeline stream code already watches its head for preview updates.

**Choice:** For now, derive preview/card state from latest `SessionObservation` rows instead of adding a new table.

**Rationale:** This removes the truth violation without adding a schema migration. A materialized `live_transcript_snapshots` table can be added later if read-time derivation becomes expensive.

**Revisit if:** Session lists need preview queries over thousands of active sessions or multiple live providers add high-frequency streams.

### Decision: Keep Legacy Columns Temporarily

**Context:** `AgentEvent` already has `event_origin` and provisional fields in deployed DBs.

**Choice:** Do not drop columns in this phase. Treat non-durable rows as legacy/hidden data and stop creating new ones.

**Rationale:** The launch-critical behavior is the read/write boundary. Dropping columns adds migration risk without improving user-facing truth.

**Revisit if:** Before launch we schedule a schema cleanup pass.

### Decision: Handle Archive Freshness Separately

**Context:** This incident also showed durable Codex JSONL ingest lagging behind live bridge output.

**Choice:** The transcript truth redesign prevents preview pollution. A separate phase strengthens Codex turn-completion wakeups and tests archive freshness paths.

**Rationale:** Combining truth-boundary cleanup with shipper scheduling would blur two different failure modes.

**Revisit if:** Tests reveal live overlay cannot be trusted without the shipper change in the same commit.

## Implementation Phases

### Phase 1: Contract And Tests

Acceptance criteria:

- durable transcript tests assert `codex_bridge_live` creates observations but no `AgentEvent`
- session event APIs return durable events only
- search/count tests prove live preview text is not searchable archive content
- preview tests prove cards can still show latest live text from observations

Test commands:

```bash
cd server && DATABASE_URL=sqlite:// uv run pytest tests_lite/test_provisional_transcript_events.py
```

### Phase 2: Server Projection

Acceptance criteria:

- `reduce_bridge_transcript_observation` no longer materializes transcript events
- `visible_transcript_event_predicate` is durable-only
- `load_active_provisional_preview_map` or its replacement reads latest bridge transcript observations
- terminal and durable-ingest reconciliation no longer depend on provisional `AgentEvent` rows
- generated API type changes are avoided unless the response contract changes

Test commands:

```bash
cd server && DATABASE_URL=sqlite:// uv run pytest tests_lite/test_provisional_transcript_events.py tests_lite/test_timeline_runtime_overlay.py tests_lite/test_session_runtime.py -q
```

### Phase 3: Codex Archive Freshness

Acceptance criteria:

- Codex turn completion explicitly wakes or enqueues archive shipping for the current transcript path
- engine tests cover turn-completed archive scheduling instead of asserting no scheduling
- live bridge observations never advance archive offsets by themselves

Test commands:

```bash
make test-engine
```

### Phase 4: End-To-End Review

Acceptance criteria:

- independent Opus review finds no truth-contract violations
- focused server and engine tests pass
- git history contains small commits for spec, server contract, and engine freshness
- branch is pushed to `origin/main`

## Non-Goals

- Build a full `live_transcript_snapshots` table now.
- Drop legacy provisional columns now.
- Redesign all provider transcript parsers.
- Make live overlay suitable for recall/search/export.

## Open Follow-Up

After this lands, inspect why manual `longhouse ship --file` reported shipping the missing tail while hosted visible event count did not change for the incident session. That is an archive ingestion/debuggability issue, not a reason to let runtime previews into archive truth.
