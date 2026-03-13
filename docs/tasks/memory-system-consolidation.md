# Memory System Consolidation Tasks

Status: In Progress
Spec: `docs/specs/memory-system-consolidation.md`
Last updated: 2026-03-12

## Phase 0: Spec

- [x] Add `TODO.md` tracking entry
- [x] Write spec with explicit keep/remove decisions
- [x] Write granular task checklist
- [ ] Commit Phase 0 artifacts

## Phase 1: Remove overlapping memory products

- [ ] Delete `oikos_memory_tools.py`
- [ ] Delete `memory_store.py`
- [ ] Remove `Memory` model and related imports/exports
- [ ] Remove old Oikos memory tool names from allowlists and generated tool contracts
- [ ] Remove `memory_strategy` from thread model, schemas, routers, frontend callers, and generated OpenAPI output
- [ ] Add legacy SQLite cleanup for `memories` table and `threads.memory_strategy`
- [ ] Commit Phase 1

## Phase 2: Gate and harden Memory Files

- [ ] Add explicit settings flags for memory-files exposure, context injection, and auto summaries
- [ ] Keep memory tools out of default Oikos/commis surfaces unless enabled
- [ ] Add shared memory path normalization/validation
- [ ] Make memory tools reject use cleanly when memory files are disabled
- [ ] Disable prompt-time memory context by default
- [ ] Disable automatic run-summary memory writes by default
- [ ] Add low-signal guards for auto-summary writes when enabled
- [ ] Commit Phase 2

## Phase 3: Docs, artifacts, and tests

- [ ] Update README / AGENTS / VISION / any stale memory docs discovered during implementation
- [ ] Update tool schema definitions and regenerate generated tool enums
- [ ] Export OpenAPI and regenerate frontend types
- [ ] Add or update focused regression coverage for memory cleanup and gating
- [ ] Run targeted verification
- [ ] Run broader verification
- [ ] Commit Phase 3

## Phase 4: Ship and verify

- [ ] Push `main`
- [ ] Wait for CI/build workflows to finish successfully
- [ ] Reprovision `david010` user instance
- [ ] Verify hosted health and live QA
- [ ] Update this task doc with final status notes
- [ ] Commit any final status/docs updates

Notes:
- Use `recall` as the public intelligence story. Memory Files stay optional and lower-profile until proven useful.
- Do not replace the deleted note-memory system with a second new abstraction. Keep the surviving memory layer boring.
