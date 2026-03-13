# Memory System Consolidation

Status: In Progress
Owner: Codex
Last updated: 2026-03-12

## Executive Summary

Longhouse currently has too many things called "memory":

- `recall` over session history
- file-backed `memory_*` tools using `memory_files`
- a separate Oikos `save_memory` / `search_memory` note store using `memories`
- a dead `memory_strategy` field on threads

That is too much product surface for OSS launch and too much mental burden for the owner. The target state is much smaller:

1. `recall` remains the primary cross-session intelligence feature
2. `Memory Files` remains as the only optional memory substrate
3. every other memory concept is removed or hidden

The surviving memory layer should feel like a virtual filesystem of durable memory files, not a vague magical memory system.

## Problem Statement

The current repo violates the "one sentence per feature" rule:

- `recall` is clear: search past sessions
- `memory_files` are plausible: curated durable files with semantic search
- Oikos note-memory is a second overlapping product
- `memory_strategy` implies configurable thread memory behavior that does not actually exist

Live prod already shows the consequence: `memory_files` are active, `memories` is effectively unused, and the automatic summary path writes obvious junk like trivial greetings and smoke-test interactions. The architecture is both redundant and noisy.

## Scope

In scope:

- builtin memory tool architecture
- Oikos / commis tool allowlists
- dead `memory_strategy` thread surface
- memory-related docs and generated contracts
- SQLite startup cleanup for removed legacy tables/columns
- targeted tests, deploy, and hosted verification

Out of scope:

- redesigning `recall`
- building a new public memory UI
- inventing a richer memory ontology
- turning memory into a launch headline feature

## Decision Log

### Decision: Keep only Memory Files as the surviving memory substrate
Context: `memory_files` is the only memory path with real production usage, prompt-time integration, and a shape that matches the desired "filesystem of memory files" model.
Choice: Retain `memory_write`, `memory_read`, `memory_ls`, `memory_search`, and `memory_delete`.
Rationale: This is the smallest memory abstraction that still looks coherent and useful.
Revisit if: A future memory design proves a better primitive than path-based durable files.

### Decision: Remove the Oikos note-memory stack entirely
Context: `save_memory`, `search_memory`, `list_memories`, and `forget_memory` introduce a second overlapping memory product with different storage and semantics.
Choice: Delete the Oikos note-memory tools, the `Memory` model, and the `memory_store` service.
Rationale: Production usage is effectively zero, and the overlap is pure cognitive load.
Revisit if: There is a clear, separately justified need for lightweight per-user notes that cannot be expressed as memory files.

### Decision: Remove `memory_strategy`
Context: Thread APIs still expose `memory_strategy="buffer"`, but there is no real strategy choice.
Choice: Remove `memory_strategy` from the model/API/frontend surface and clean legacy SQLite columns where feasible.
Rationale: Dead configuration creates false product meaning and dirty generated contracts.
Revisit if: Thread memory behavior becomes a real configurable feature again.

### Decision: Make Memory Files opt-in and low-blast-radius by default
Context: The owner wants memory available as a drop-in module, not as ambient complexity.
Choice: Gate memory-files tool exposure, prompt-time memory context, and automatic run summaries behind explicit feature flags. Default all of them off.
Rationale: This preserves the subsystem without forcing it into everyday product behavior.
Revisit if: Memory Files become a proven, core product capability.

### Decision: Add memory path validation
Context: Memory files are a virtual filesystem, but current code accepts arbitrary raw path strings.
Choice: Normalize and validate memory paths/prefixes centrally; reject absolute paths, traversal, empty segments, and other malformed inputs.
Rationale: Filesystem semantics without path hygiene is sloppy and makes future behavior harder to trust.
Revisit if: The storage model stops being path-based.

### Decision: Keep automatic summaries only as a guarded optional behavior
Context: The current summary writer is the main source of junk in `memory_files`.
Choice: Keep the subsystem available, but disable it by default and add basic low-signal skipping when enabled.
Rationale: Automatic memory formation is only worth keeping if it stays curated enough to be useful.
Revisit if: A stronger memory-formation policy or review loop lands later.

## Target Behavior

### Public/Product Story

Longhouse publicly talks about:

- session search
- recall across sessions
- optional memory files later, if enabled

Longhouse does not publicly talk about:

- multiple memory systems
- Oikos note-memory
- thread memory strategies

### Tool Surfaces

By default:

- Oikos does not get memory tools
- commis does not get memory tools
- prompt-time memory context injection is off
- automatic memory summaries are off

When explicitly enabled:

- Oikos and commis may receive the `memory_*` file tools
- memory context injection may be enabled separately
- automatic summary writing may be enabled separately

### Data Model

Must keep:

- `memory_files`
- `memory_embeddings`

Must remove:

- `memories`
- `memory_strategy`

## Acceptance Criteria

1. The Oikos note-memory code path is removed from code, tool contracts, tests, and docs.
2. `memory_strategy` is removed from backend schemas, routers, frontend API types, and generated OpenAPI output.
3. `Memory Files` remain as the only memory tool family.
4. Memory-files tool exposure is disabled by default and controlled explicitly by settings/env flags.
5. Prompt-time memory context injection is disabled by default and controlled explicitly.
6. Automatic memory summary writing is disabled by default and controlled explicitly.
7. Memory file path handling is normalized and validated centrally.
8. SQLite startup cleanup removes the legacy `memories` table and the dead `threads.memory_strategy` column on existing instances.
9. Docs and generated artifacts describe one optional memory-files subsystem, not multiple memory products.
10. Hosted deploy succeeds and the primary dev instance remains healthy after reprovision.

## Implementation Phases

### Phase 0: Spec and task tracking

Deliverables:

- `TODO.md` entry
- this spec
- task checklist

Acceptance:

- artifacts committed

### Phase 1: Remove overlapping memory products

Deliverables:

- delete Oikos note-memory tools/service/model
- remove old tool names from allowlists, schemas, and generated tool contracts
- remove `memory_strategy` from thread surfaces
- clean legacy SQLite schema on startup

Acceptance:

- only Memory Files remain

### Phase 2: Gate and harden Memory Files

Deliverables:

- explicit settings flags for memory-files exposure/context/auto-summary
- path normalization and validation helpers
- low-signal summary skip logic
- tool/runtime behavior respects the new flags

Acceptance:

- memory stays modular and off by default

### Phase 3: Docs, tests, and artifacts

Deliverables:

- docs updated
- tool definitions regenerated
- OpenAPI/types regenerated
- targeted and broader verification run

Acceptance:

- no stale references remain in normal product surfaces

### Phase 4: Ship and verify

Deliverables:

- push `main`
- wait for CI
- reprovision hosted instance
- verify health and live QA

Acceptance:

- hosted instance healthy and checks green
