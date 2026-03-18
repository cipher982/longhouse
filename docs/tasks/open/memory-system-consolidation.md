# Memory System Consolidation Tasks

Status: In Progress
Spec: `docs/specs/memory-system-consolidation.md`
Last updated: 2026-03-12

## Phase 0: Spec

- [x] Add `TODO.md` tracking entry
- [x] Write spec with explicit keep/remove decisions
- [x] Write granular task checklist
- [x] Commit Phase 0 artifacts

## Phase 1: Remove overlapping memory products

- [x] Delete `oikos_memory_tools.py`
- [x] Delete `memory_store.py`
- [x] Remove `Memory` model and related imports/exports
- [x] Remove old Oikos memory tool names from allowlists and generated tool contracts
- [x] Remove `memory_strategy` from thread model, schemas, routers, frontend callers, and generated OpenAPI output
- [x] Add legacy SQLite cleanup for `memories` table and `threads.memory_strategy`
- [x] Commit Phase 1

## Phase 2: Gate and harden Memory Files

- [x] Add explicit settings flags for memory-files exposure, context injection, and auto summaries
- [x] Keep memory tools out of default Oikos/commis surfaces unless enabled
- [x] Add shared memory path normalization/validation
- [x] Make memory tools reject use cleanly when memory files are disabled
- [x] Disable prompt-time memory context by default
- [x] Disable automatic run-summary memory writes by default
- [x] Add low-signal guards for auto-summary writes when enabled
- [x] Commit Phase 2

## Phase 3: Docs, artifacts, and tests

- [x] Update README / AGENTS / VISION / any stale memory docs discovered during implementation
- [x] Update tool schema definitions and regenerate generated tool enums
- [x] Export OpenAPI and regenerate frontend types
- [x] Add or update focused regression coverage for memory cleanup and gating
- [x] Run targeted verification
- [x] Run broader verification
- [x] Commit Phase 3

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
- Local verification complete before ship: focused memory/tool tests, `make test`, `make test-e2e`, and `bun run validate:types`.
