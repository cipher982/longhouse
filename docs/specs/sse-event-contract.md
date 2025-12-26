# SSE Event Contract System

**Status:** Draft
**Created:** 2024-12-26
**Protocol:** SDP-1

## Executive Summary

Add a typed contract system for Server-Sent Events (SSE) between the Jarvis backend and frontend, mirroring the existing WebSocket contract system. This eliminates runtime mismatches (like the `id=null` issue) by making schema violations compile-time errors.

### Problem

SSE events between backend (`jarvis_sse.py`) and frontend (`supervisor-chat-controller.ts`) are defined ad-hoc in two separate codebases with no shared schema. This leads to:

1. **Silent drift** - Backend adds fields frontend doesn't expect (or vice versa)
2. **Missing fields** - Backend omits fields frontend expects (e.g., SSE `id:` field)
3. **Type mismatches** - Field types differ between Python and TypeScript
4. **No compile-time checks** - Errors only surface at runtime

### Solution

Create `schemas/sse-events.asyncapi.yml` as the single source of truth, then generate:
- **Python**: Pydantic models + typed emitter for backend
- **TypeScript**: Interfaces + type guards for frontend
- **Pre-commit check**: Drift detection blocks commits when out of sync

---

## Decision Log

### Decision: Use AsyncAPI 3.0 Format
**Context:** Need a schema format for SSE events
**Choice:** AsyncAPI 3.0 (same as WebSocket schema)
**Rationale:** Consistent tooling, team familiarity, existing generator can be extended
**Revisit if:** AsyncAPI proves too complex for SSE's simpler model

### Decision: SSE Envelope Structure
**Context:** SSE protocol has `event:`, `data:`, `id:` fields
**Choice:** Define explicit envelope with all three fields as first-class schema elements
**Rationale:** The `id=null` bug happened because `id:` wasn't in the contract
**Revisit if:** SSE spec changes or we need retry fields

### Decision: Separate Schema File (Not Merge with WS)
**Context:** Could add SSE events to existing `ws-protocol-asyncapi.yml`
**Choice:** Create separate `sse-events.asyncapi.yml`
**Rationale:** SSE and WS have different transports, lifecycles, and consumers. Separation prevents accidental coupling.
**Revisit if:** Significant schema duplication emerges

### Decision: Generate Into Existing `generated/` Directories
**Context:** Where to put generated code
**Choice:** Same locations as WS: `backend/zerg/generated/sse_events.py`, `frontend/src/generated/sse-events.ts`
**Rationale:** Consistent with existing patterns, already git-ignored appropriately
**Revisit if:** Generated files conflict

---

## Architecture

### Current State (No Contract)

```
Backend (Python)                    Frontend (TypeScript)
─────────────────                   ────────────────────
jarvis_sse.py                       supervisor-chat-controller.ts
  yield {                             if (eventType === 'supervisor_started') {
    "event": "supervisor_started",      // Expects payload.run_id
    "data": json.dumps({...})           // But what if backend changes it?
  }                                   }
       │                                     │
       └──── No shared contract ─────────────┘
             (Runtime errors only)
```

### Target State (With Contract)

```
                    schemas/sse-events.asyncapi.yml
                         (Source of Truth)
                               │
                    ┌──────────┴──────────┐
                    │   make regen-sse    │
                    └──────────┬──────────┘
                               │
          ┌────────────────────┴────────────────────┐
          ▼                                         ▼
backend/zerg/generated/sse_events.py    frontend/src/generated/sse-events.ts
  - SSEEnvelope (Pydantic)                - SSEEnvelope (interface)
  - SupervisorStartedPayload              - SupervisorStartedPayload
  - emit_sse_event() typed emitter        - SSEEventMap discriminated union
          │                                         │
          ▼                                         ▼
jarvis_sse.py                           supervisor-chat-controller.ts
  from zerg.generated.sse_events          import { SupervisorStartedPayload }
  emit_sse_event(                         const payload: SupervisorStartedPayload
    "supervisor_started",                 // TypeScript error if wrong type
    SupervisorStartedPayload(...)
  )
```

### SSE Envelope Schema

```yaml
SSEEnvelope:
  type: object
  required: [event, data]
  properties:
    event:
      type: string
      description: SSE event type (maps to `event:` line)
    id:
      type: integer
      description: Event ID for resumption (maps to `id:` line)
    data:
      type: object
      description: JSON payload (maps to `data:` line)
```

### Event Types to Define

| Event | Direction | Purpose |
|-------|-----------|---------|
| `connected` | S→C | Initial connection confirmation |
| `heartbeat` | S→C | Keep-alive ping |
| `supervisor_started` | S→C | Supervisor began processing |
| `supervisor_thinking` | S→C | Supervisor reasoning phase |
| `supervisor_token` | S→C | LLM token stream (high frequency) |
| `supervisor_complete` | S→C | Supervisor finished |
| `supervisor_deferred` | S→C | Timeout migration (v2.2) |
| `error` | S→C | Execution error |
| `worker_spawned` | S→C | Worker created |
| `worker_started` | S→C | Worker began execution |
| `worker_complete` | S→C | Worker finished |
| `worker_summary_ready` | S→C | Worker summary extracted |
| `worker_tool_started` | S→C | Worker tool call began |
| `worker_tool_completed` | S→C | Worker tool call succeeded |
| `worker_tool_failed` | S→C | Worker tool call failed |

---

## Implementation Phases

### Phase 1: Create AsyncAPI Schema

**Goal:** Define all SSE events in `schemas/sse-events.asyncapi.yml`

**Acceptance Criteria:**
- [ ] Schema file exists at `schemas/sse-events.asyncapi.yml`
- [ ] All 15 event types defined with full payload schemas
- [ ] SSE envelope structure defined (event, id, data)
- [ ] Schema validates with AsyncAPI tooling
- [ ] Common types extracted (UsageData, WorkerRef, etc.)

**Test:** `npx @asyncapi/cli validate schemas/sse-events.asyncapi.yml`

---

### Phase 2: Create Code Generator

**Goal:** Script to generate Python and TypeScript from schema

**Acceptance Criteria:**
- [ ] Script at `scripts/generate-sse-types.py`
- [ ] Generates `apps/zerg/backend/zerg/generated/sse_events.py`:
  - Pydantic models for each payload type
  - `SSEEventType` enum
  - `emit_sse_event()` typed emitter function
- [ ] Generates `apps/zerg/frontend-web/src/generated/sse-events.ts`:
  - TypeScript interfaces for each payload type
  - `SSEEventType` string literal union
  - `SSEEventMap` discriminated union
- [ ] Generator handles:
  - Optional fields (`?` in TS, `Optional[]` in Python)
  - Nested objects (Usage, WorkerRef)
  - Enums/literals for status fields

**Test:** `python scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml` produces valid files

---

### Phase 3: Add Make Target and Pre-commit

**Goal:** Integrate into build system with drift detection

**Acceptance Criteria:**
- [ ] `make regen-sse` regenerates SSE types
- [ ] `make validate-sse` checks for drift (like `validate-ws`)
- [ ] Pre-commit hook runs drift check
- [ ] `.pre-commit-config.yaml` updated
- [ ] `Makefile` updated with new targets
- [ ] AGENTS.md updated with new commands

**Test:**
1. Modify schema, run `make validate-sse` → fails
2. Run `make regen-sse`, then `make validate-sse` → passes
3. Commit with drift → blocked by pre-commit

---

### Phase 4: Wire Up Backend

**Goal:** Backend uses generated types for SSE emission

**Acceptance Criteria:**
- [ ] `jarvis_sse.py` imports from `zerg.generated.sse_events`
- [ ] All `yield` statements use typed payloads
- [ ] `emit_run_event()` uses generated types
- [ ] SSE `id:` field populated from `event_id`
- [ ] Backend tests pass
- [ ] No runtime type errors

**Test:** `make test` passes, manual test shows `id=<number>` in logs (not `id=null`)

---

### Phase 5: Wire Up Frontend

**Goal:** Frontend uses generated types for SSE handling

**Acceptance Criteria:**
- [ ] `supervisor-chat-controller.ts` imports from `generated/sse-events`
- [ ] `handleSSEEvent()` uses discriminated union for type safety
- [ ] All payload access is type-checked
- [ ] TypeScript compilation passes with strict mode
- [ ] Frontend tests pass

**Test:** `make test` passes, TypeScript catches a deliberately wrong field access

---

### Phase 6: Documentation and Cleanup

**Goal:** Update docs, remove dead code

**Acceptance Criteria:**
- [ ] AGENTS.md documents `make regen-sse` and `make validate-sse`
- [ ] Pre-commit section updated
- [ ] Remove any ad-hoc TypeScript interfaces replaced by generated code
- [ ] Remove any ad-hoc Python types replaced by generated code
- [ ] This spec marked as "Implemented"

**Test:** Fresh clone, `make dev`, `make test-all` passes

---

## Files Changed

| File | Change |
|------|--------|
| `schemas/sse-events.asyncapi.yml` | **NEW** - SSE event schema |
| `scripts/generate-sse-types.py` | **NEW** - Code generator |
| `scripts/regen-sse-code.sh` | **NEW** - Shell wrapper |
| `apps/zerg/backend/zerg/generated/sse_events.py` | **NEW** - Generated Python |
| `apps/zerg/frontend-web/src/generated/sse-events.ts` | **NEW** - Generated TypeScript |
| `apps/zerg/backend/zerg/routers/jarvis_sse.py` | MODIFY - Use generated types |
| `apps/zerg/backend/zerg/services/event_store.py` | MODIFY - Use generated types |
| `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts` | MODIFY - Use generated types |
| `Makefile` | MODIFY - Add `regen-sse`, `validate-sse` |
| `.pre-commit-config.yaml` | MODIFY - Add SSE drift check |
| `AGENTS.md` | MODIFY - Document new commands |

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Generator complexity | Start with subset of events, expand incrementally |
| Breaking existing code | Phase 4-5 are pure refactors; types match current runtime behavior |
| Schema/code drift in PR | Pre-commit hook catches before merge |
| AsyncAPI validation failures | Use same AsyncAPI version as WS schema (3.0.0) |

---

## Out of Scope

- Resumable SSE implementation (separate feature, but this enables it)
- WebSocket migration to use same events
- Client-to-server SSE (not supported by protocol)
- Event versioning (can add later with `x-version` extension)
